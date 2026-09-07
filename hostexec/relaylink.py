import queue
import subprocess
import threading
import time
import traceback

from . import protocol

PING_INTERVAL = 5.0
STABLE_SECONDS = 30.0


def exec_command(engine, container, mount):
    return [engine, "exec", "-i", container, f"{mount}/relay"]


class LinkSession:
    def __init__(self, link, ident, header):
        self.link = link
        self.ident = ident
        self.header = header
        self.incoming = queue.Queue()
        self.buffer = b""
        self.eof = False
        self.answered = False
        self.closed = False
        self.remote_closed = False

    def feed(self, payload):
        self.incoming.put(payload)

    def _fill(self):
        while not self.buffer and not self.eof:
            chunk = self.incoming.get()
            if chunk is None:
                self.eof = True
            else:
                self.buffer = chunk

    def read(self, size=-1):
        self._fill()
        if size < 0 or size >= len(self.buffer):
            chunk, self.buffer = self.buffer, b""
        else:
            chunk, self.buffer = self.buffer[:size], self.buffer[size:]
        return chunk

    def readline(self):
        if self.header is not None:
            line, self.header = self.header, None
            return line
        line = b""
        while not line.endswith(b"\n"):
            chunk = self.read(1)
            if not chunk:
                break
            line += chunk
        return line

    def write(self, data):
        if not self.closed and not self.remote_closed:
            kind = protocol.DATA if self.answered else protocol.ACCEPT
            self.answered = True
            self.link.send(kind, self.ident, data)
        return len(data)

    def flush(self):
        pass

    def remote_close(self):
        self.remote_closed = True
        self.feed(None)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if not self.remote_closed:
            self.link.send(protocol.CLOSE, self.ident)
        self.feed(None)
        self.link.forget(self.ident)


class Link:
    def __init__(self, command, server, log, alive=None, restarts=3, delay=1.0, env=None):
        self.command = command
        self.env = env
        self.server = server
        self.log = log
        self.alive = alive or (lambda: True)
        self.restarts = restarts
        self.delay = delay
        self.proc = None
        self.stopping = False
        self.sessions = {}
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.threads = []

    def start(self):
        self._spawn()
        self._thread(self._supervise)
        self._thread(self._ping)

    def _thread(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self.threads.append(thread)
        return thread

    def _spawn(self):
        self.proc = subprocess.Popen(
            self.command, env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.log(f"relay started: {' '.join(self.command)} (pid {self.proc.pid})")
        self._thread(self._read, self.proc)
        self._thread(self._copy_stderr, self.proc)

    def send(self, kind, ident, payload=b""):
        frame = protocol.pack_link(kind, ident, payload)
        with self.write_lock:
            proc = self.proc
            if proc is None or proc.stdin is None or proc.stdin.closed:
                return False
            try:
                proc.stdin.write(frame)
                proc.stdin.flush()
            except OSError as error:
                self.log(f"writing to the relay failed: {error}")
                return False
        return True

    def forget(self, ident):
        with self.lock:
            self.sessions.pop(ident, None)

    def _read(self, proc):
        stream = proc.stdout
        while True:
            try:
                frame = protocol.read_link(stream)
            except protocol.ProtocolError as error:
                self.log(f"bad frame from the relay: {error}")
                break
            if frame is None:
                break
            kind, ident, payload = frame
            if kind == protocol.NEW:
                session = LinkSession(self, ident, payload)
                with self.lock:
                    self.sessions[ident] = session
                self._thread(self._serve, session)
            elif kind == protocol.DATA:
                with self.lock:
                    session = self.sessions.get(ident)
                if session is not None:
                    session.feed(payload)
            elif kind == protocol.CLOSE:
                with self.lock:
                    session = self.sessions.pop(ident, None)
                if session is not None:
                    session.remote_close()
            elif kind != protocol.PING:
                self.log(f"unknown link frame {kind!r}")
        stream.close()
        self._drop_sessions()

    def _serve(self, session):
        try:
            self.server.handle(session)
        except Exception:
            self.log("session failed:\n" + traceback.format_exc())
        finally:
            session.close()

    def _copy_stderr(self, proc):
        for line in proc.stderr:
            self.log(line.decode("utf-8", "replace").rstrip())
        proc.stderr.close()

    def _drop_sessions(self):
        with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            session.remote_close()

    def _supervise(self):
        failures = 0
        while True:
            proc = self.proc
            started = time.monotonic()
            proc.wait()
            self._drop_sessions()
            if self.stopping:
                return
            if time.monotonic() - started > STABLE_SECONDS:
                failures = 0
            failures += 1
            self.log(f"relay exited with status {proc.returncode}")
            if failures > self.restarts:
                self.log("relay keeps failing, host exec is off for the rest of the session")
                return
            if not self.alive():
                self.log("container is gone, not restarting the relay")
                return
            time.sleep(self.delay)
            with self.write_lock:
                if self.stopping:
                    return
                self._spawn()

    def _ping(self):
        while not self.stopping:
            time.sleep(PING_INTERVAL)
            with self.lock:
                idle = not self.sessions
            if idle and not self.stopping:
                self.send(protocol.PING, 0)

    def stop(self):
        self.stopping = True
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.close()
        with self.write_lock:
            proc = self.proc
            if proc is None:
                return
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self.log("relay stopped")
