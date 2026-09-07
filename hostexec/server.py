import os
import signal
import subprocess
import sys
import threading
import time

from . import protocol

CHUNK = 65536
KILL_GRACE = 5.0
DRAIN_GRACE = 5.0
STRIPPED_VARS = ("CLAUDE_HOST_EXEC", "CLAUDE_HOST_MODULE")
DEFAULT_ENVIRONMENT = {
    "CI": "1",
    "FORCE_COLOR": "0",
    "NO_COLOR": "1",
    "PYTHONIOENCODING": "utf-8",
    "DOTNET_CLI_UI_LANGUAGE": "en",
}


def child_environment(module):
    environment = {key: value for key, value in os.environ.items() if key not in STRIPPED_VARS}
    environment.update(DEFAULT_ENVIRONMENT)
    environment.update(getattr(module, "ENVIRONMENT", {}))
    return environment


def spawn_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class Server:
    def __init__(self, module, cwd, log_path):
        self.module = module
        self.cwd = cwd
        self.log_path = log_path
        self.log_file = None
        self.log_lock = threading.Lock()
        self.busy = threading.Lock()
        self.current = None

    def start(self):
        if self.log_path is not None:
            self.log_file = open(self.log_path, "a", encoding="utf-8")
        self.log(f"serving {self.module.NAME} in {self.cwd}: {self.module.describe(self.cwd)}")

    def stop(self):
        proc = self.current
        if proc is not None:
            self.log("stopping, killing the running command")
            kill(proc)
        self.log("stopped")
        with self.log_lock:
            if self.log_file is not None:
                self.log_file.close()
                self.log_file = None

    def log(self, message):
        with self.log_lock:
            if self.log_file is None:
                return
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            self.log_file.write(f"{stamp} {message}\n")
            self.log_file.flush()

    def handle(self, session):
        try:
            header = protocol.read_header(session)
        except protocol.ProtocolError as error:
            self.log(f"rejected session: {error}")
            return
        verb = header.get("verb")
        args = header.get("args") or []
        if not isinstance(verb, str) or not isinstance(args, list) \
                or not all(isinstance(arg, str) for arg in args):
            self._answer(session, {"ok": False, "error": "malformed request"})
            return
        if verb == "list":
            self._answer(session, {"ok": True, "verbs": self.module.describe(self.cwd)})
            return
        try:
            argv = self.module.translate(self.cwd, verb, args)
        except Exception as error:
            self.log(f"refused {verb} {args}: {error!r}")
            self._answer(session, {"ok": False, "error": str(error)})
            return
        if not self.busy.acquire(blocking=False):
            self.log(f"refused {verb}: busy")
            self._answer(session, {"ok": False, "error": "busy: another command is still running"})
            return
        try:
            self._run(session, verb, argv)
        finally:
            self.busy.release()

    def _answer(self, session, message):
        try:
            session.write(protocol.encode_header(message))
            session.flush()
        except (OSError, ValueError):
            pass

    def _run(self, session, verb, argv):
        try:
            proc = subprocess.Popen(
                argv, cwd=str(self.cwd), env=child_environment(self.module),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, **spawn_options())
        except OSError as error:
            self.log(f"cannot start {argv}: {error}")
            self._answer(session, {"ok": False, "error": f"cannot start {argv[0]}: {error}"})
            return
        self.current = proc
        self.log(f"{verb}: pid {proc.pid} {argv}")
        self._answer(session, {"ok": True, "argv": argv, "cwd": str(self.cwd)})

        write_lock = threading.Lock()

        def emit(kind, payload):
            with write_lock:
                try:
                    session.write(protocol.pack_frame(kind, payload))
                    session.flush()
                except (OSError, ValueError):
                    pass

        pumps = [
            threading.Thread(target=self._pump, args=(proc.stdout, protocol.STDOUT, emit), daemon=True),
            threading.Thread(target=self._pump, args=(proc.stderr, protocol.STDERR, emit), daemon=True),
        ]
        for pump in pumps:
            pump.start()
        threading.Thread(target=self._control, args=(session, proc), daemon=True).start()

        proc.wait()
        deadline = time.monotonic() + DRAIN_GRACE
        for pump in pumps:
            pump.join(max(0.0, deadline - time.monotonic()))
            if pump.is_alive():
                self.log(f"pid {proc.pid}: output pipe still open after exit, a child keeps it")
        self.current = None
        try:
            proc.stdin.close()
        except OSError:
            pass
        self.log(f"{verb}: pid {proc.pid} exited with {proc.returncode}")
        emit(protocol.EXIT, protocol.pack_status(proc.returncode))

    def _pump(self, stream, kind, emit):
        while True:
            chunk = stream.read1(CHUNK) if hasattr(stream, "read1") else stream.read(CHUNK)
            if not chunk:
                stream.close()
                return
            emit(kind, chunk)

    def _control(self, session, proc):
        while True:
            try:
                frame = protocol.read_frame(session)
            except protocol.ProtocolError as error:
                self.log(f"pid {proc.pid}: bad frame from the client: {error}")
                frame = None
            except (OSError, ValueError):
                frame = None
            if frame is None:
                if proc.poll() is None:
                    self.log(f"pid {proc.pid}: client went away, killing")
                    kill(proc)
                return
            kind, payload = frame
            if kind == protocol.STDIN:
                try:
                    proc.stdin.write(payload)
                    proc.stdin.flush()
                except OSError:
                    pass
            elif kind == protocol.STDIN_EOF:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            elif kind == protocol.KILL:
                self.log(f"pid {proc.pid}: kill requested by the client")
                kill(proc)
