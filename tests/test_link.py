import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from hostexec import protocol, relaylink, server

ROOT = Path(__file__).resolve().parent.parent
RELAY = ROOT / "hostexec" / "container" / "relay"
HOSTRUN = ROOT / "hostexec" / "container" / "hostrun"


class EchoServer:
    def handle(self, session):
        header = protocol.read_header(session)
        session.write(protocol.encode_header({"ok": True, "verb": header["verb"]}))
        while True:
            frame = protocol.read_frame(session)
            if frame is None:
                return
            kind, payload = frame
            session.write(protocol.pack_frame(kind, payload[::-1]))


class NpmModule:
    NAME = "npm"

    def translate(self, repo, verb, args):
        scripts = json.loads((repo / "package.json").read_text())["scripts"]
        if verb not in scripts:
            raise ValueError(f"no {verb!r} script")
        return [sys.executable, "-c", scripts[verb]]

    def describe(self, repo):
        return {"build": "fake build", "test": "fake test"}


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "needs unix sockets")
class LinkCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.socket = str(self.tmp / "sock")
        self.env = dict(os.environ, CLAUDE_HOST_EXEC=self.socket)
        self.logs = []

    def start_link(self, target, **options):
        link = relaylink.Link([sys.executable, str(RELAY)], target, self.logs.append, env=self.env, **options)
        link.start()
        self.addCleanup(link.stop)
        for _ in range(100):
            if os.path.exists(self.socket):
                return link
            time.sleep(0.05)
        self.fail("relay did not listen")

    def connect(self):
        for _ in range(40):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.socket)
                self.addCleanup(sock.close)
                return sock
            except OSError:
                sock.close()
                time.sleep(0.1)
        self.fail("cannot connect to the relay")


class LinkTest(LinkCase):
    def test_interleaved_sessions_and_binary_payloads(self):
        self.start_link(EchoServer())
        payloads = {1: bytes(range(256)) * 200, 2: b"\r\n\x00\x1a" * 5000}
        failures = []

        def client(ident):
            try:
                sock = self.connect()
                reader = sock.makefile("rb")
                sock.sendall(protocol.encode_header({"verb": f"c{ident}"}))
                self.assertEqual(protocol.read_header(reader)["verb"], f"c{ident}")
                for _ in range(10):
                    sock.sendall(protocol.pack_frame(protocol.STDIN, payloads[ident]))
                    self.assertEqual(protocol.read_frame(reader), (protocol.STDIN, payloads[ident][::-1]))
                sock.close()
            except Exception as error:
                failures.append(error)

        threads = [threading.Thread(target=client, args=(ident,)) for ident in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(failures, [])

    def test_relay_restart(self):
        link = self.start_link(EchoServer(), delay=0.1)
        first = link.proc
        sock = self.connect()
        reader = sock.makefile("rb")
        sock.sendall(protocol.encode_header({"verb": "x"}))
        protocol.read_header(reader)
        first.kill()
        self.assertIsNone(protocol.read_frame(reader))
        for _ in range(100):
            if link.proc is not first and os.path.exists(self.socket):
                break
            time.sleep(0.05)
        sock = self.connect()
        reader = sock.makefile("rb")
        sock.sendall(protocol.encode_header({"verb": "again"}))
        self.assertEqual(protocol.read_header(reader)["verb"], "again")
        self.assertTrue(any("relay exited" in line for line in self.logs))


class HostrunTest(LinkCase):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "package.json").write_text(json.dumps({"scripts": {
            "build": "import sys; sys.stdout.write('built ' + sys.stdin.read()); sys.stderr.write('warn'); sys.exit(3)",
            "test": "import time; time.sleep(30)",
        }}))
        self.server = server.Server(NpmModule(), self.repo, None)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.start_link(self.server)

    def hostrun(self, *args, **options):
        return subprocess.run([sys.executable, str(HOSTRUN), *args], env=self.env, capture_output=True, **options)

    def test_build_relays_everything(self):
        result = self.hostrun("build", input=b"input")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, b"built input")
        self.assertTrue(result.stderr.startswith(b"hostrun: "))
        self.assertTrue(result.stderr.endswith(b"warn"))

    def test_list_and_refusals(self):
        self.assertEqual(self.hostrun("--list").stdout, b"build   fake build\ntest    fake test\n")
        result = self.hostrun("deploy")
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"'deploy'", result.stderr)
        self.assertEqual(self.hostrun().returncode, 2)

    def test_unset_variable(self):
        env = {key: value for key, value in os.environ.items() if key != "CLAUDE_HOST_EXEC"}
        result = subprocess.run([sys.executable, str(HOSTRUN), "build"], env=env, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"not enabled", result.stderr)

    def test_signal_kills_host_process(self):
        proc = subprocess.Popen([sys.executable, str(HOSTRUN), "test"], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(100):
            if self.server.current is not None:
                break
            time.sleep(0.05)
        child = self.server.current
        proc.terminate()
        proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 143)
        self.assertIsNotNone(child.poll())
        self.assertIsNone(self.server.current)


if __name__ == "__main__":
    unittest.main()
