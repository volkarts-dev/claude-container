import socket
import sys
import threading
import time
import unittest
from pathlib import Path

from hostexec import protocol, server

SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]
ECHO = [sys.executable, "-c",
        "import sys; data = sys.stdin.read(); sys.stdout.write('out:' + data); "
        "sys.stderr.write('err:' + data); sys.exit(7)"]


class FakeModule:
    NAME = "fake"
    ENVIRONMENT = {"HOSTEXEC_TEST": "yes"}

    def translate(self, repo, verb, args):
        if verb == "sleep":
            return SLEEP
        if verb == "echo":
            return ECHO
        if verb == "env":
            return [sys.executable, "-c", "import os; print(os.environ['HOSTEXEC_TEST'], os.environ['CI'])"]
        if verb == "missing":
            return ["/nonexistent/tool"]
        raise ValueError(f"unknown verb {verb!r}")

    def describe(self, repo):
        return {"echo": "echo", "sleep": "sleep"}


class Client:
    def __init__(self, case, srv):
        self.sock, peer = socket.socketpair()
        case.addCleanup(self.close)
        self.reader = self.sock.makefile("rb")
        self.session = peer.makefile("rwb")
        self.peer = peer
        self.thread = threading.Thread(target=self._serve, args=(srv,), daemon=True)
        self.thread.start()

    def _serve(self, srv):
        try:
            srv.handle(self.session)
        finally:
            try:
                self.session.close()
            except OSError:
                pass
            self.peer.close()

    def request(self, verb, args=()):
        self.sock.sendall(protocol.encode_header({"verb": verb, "args": list(args)}))
        return protocol.read_header(self.reader)

    def send(self, kind, payload=b""):
        self.sock.sendall(protocol.pack_frame(kind, payload))

    def collect(self):
        out, err, status = b"", b"", None
        while True:
            frame = protocol.read_frame(self.reader)
            if frame is None:
                break
            kind, payload = frame
            if kind == protocol.STDOUT:
                out += payload
            elif kind == protocol.STDERR:
                err += payload
            elif kind == protocol.EXIT:
                status = protocol.unpack_status(payload)
                break
        self.thread.join(5)
        return out, err, status

    def close(self):
        self.reader.close()
        self.sock.close()


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.server = server.Server(FakeModule(), Path.cwd(), None)
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_streams_status_and_stdin(self):
        client = Client(self, self.server)
        answer = client.request("echo")
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["argv"], ECHO)
        client.send(protocol.STDIN, b"hello")
        client.send(protocol.STDIN_EOF)
        out, err, status = client.collect()
        self.assertEqual((out, err, status), (b"out:hello", b"err:hello", 7))

    def test_list(self):
        answer = Client(self, self.server).request("list")
        self.assertEqual(answer, {"ok": True, "verbs": {"echo": "echo", "sleep": "sleep"}})

    def test_refusals(self):
        self.assertEqual(Client(self, self.server).request("deploy")["error"], "unknown verb 'deploy'")
        self.assertFalse(Client(self, self.server).request("echo", [1])["ok"])
        answer = Client(self, self.server).request("missing")
        self.assertFalse(answer["ok"])
        self.assertIn("cannot start", answer["error"])

    def test_environment(self):
        client = Client(self, self.server)
        client.request("env")
        client.send(protocol.STDIN_EOF)
        out, _, status = client.collect()
        self.assertEqual((out.strip(), status), (b"yes 1", 0))

    def test_busy_and_kill(self):
        first = Client(self, self.server)
        self.assertTrue(first.request("sleep")["ok"])
        second = Client(self, self.server)
        self.assertEqual(second.request("sleep")["error"], "busy: another command is still running")
        first.send(protocol.KILL)
        _, _, status = first.collect()
        self.assertNotEqual(status, 0)
        self.assertIsNone(self.server.current)
        third = Client(self, self.server)
        self.assertTrue(third.request("sleep")["ok"])
        third.send(protocol.KILL)
        third.collect()

    def test_disconnect_kills(self):
        client = Client(self, self.server)
        client.request("sleep")
        proc = self.server.current
        client.close()
        for _ in range(100):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        self.assertIsNotNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
