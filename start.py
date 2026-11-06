#!/usr/bin/env python3
import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROXY_LISTEN = 3128
DEV_PREFIX = "claude-dev"
ORPHAN_GRACE = 60
HOST_ALIASES = {
    "localhost", "127.0.0.1", "::1", "[::1]",
    "host.docker.internal", "host.containers.internal",
}
PASSTHROUGH_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
)
FLAG_OPTIONS = {"-h", "--help", "-s", "--shell"}
VALUE_OPTIONS = {"-e", "--env", "--host-port"}

DESCRIPTION = """\
Starts an egress proxy for this session, then claude in a container. The
current directory is always mounted and used as the working directory. Each
additional PATH is mounted read-write under /workspace, named after the last
component of that path.

The first option this script does not know, and everything after it, is
passed on to claude itself, or to bash with -s. Options of this script and
PATHs have to come before it. Anything after -- is passed on as well, which
also covers arguments that do not start with a dash or clash with an option
of this script, like a prompt or claude's own --help.

Environment variables reach the container only when named: -e NAME forwards
the host's value (nothing is set when it is unset on the host), -e NAME=VALUE
sets one explicitly. CLAUDE_ENV names host variables to forward on every start.

Networking
  The claude container runs on an internal container network with no route off
  the host. Its only way out is the tinyproxy container, which sits on that
  network as http://proxy:3128 and on a second, outward-facing network. That
  proxy goes out over the host connection, or forwards to the corporate proxy
  named by CLAUDE_PROXY, except for the hosts named by CLAUDE_NO_PROXY. It
  passes plain HTTP to port 80 and CONNECT to port 443 only.

  Every start gets its own internal network and proxy container, which are
  removed together with the claude container when the session ends. Proxies
  and networks whose session is gone, because this script was killed before it
  could clean up, are removed on the next start.

  The proxy image is built on demand; the dev image has to be built beforehand
  with build.py.

Host ports
  Each --host-port PORT (or entry in CLAUDE_HOST_PORTS) makes a TCP port of the
  host reachable as localhost:PORT inside the claude container. A forwarder in
  the claude container hands the connection to the proxy container, where a
  second one passes it on to the host gateway on the same port. Only the named
  ports are forwarded, and only to the host. The service on the host has to
  listen on an address the host gateway reaches, which on a Linux engine is
  not the loopback interface. Where the host gateway does not lead to the
  machine the service runs on, like Windows behind a podman machine in WSL2,
  CLAUDE_HOST_TARGET names the address to forward to instead."""

EPILOG = """\
environment:
  CONTAINER_ENGINE container frontend, docker or podman (default docker)
  CLAUDE_PROXY     corporate proxy the egress forwards to, e.g.
                   http://proxy.corp:3128 or http://user:pass@host:port; falls
                   back to HTTPS_PROXY/HTTP_PROXY, unset means direct
  CLAUDE_NO_PROXY  comma-separated hosts, domains (.corp.example) and networks
                   (10.0.0.0/8) the proxy reaches directly instead of through
                   the upstream; falls back to NO_PROXY
  CLAUDE_NET       internal network name prefix, the session id is appended
                   (default claude-internal)
  CLAUDE_BRIDGE    outward-facing network the proxy reaches the outside on,
                   created when missing (default claude-egress); it has to
                   carry DNS, which the engines' predefined bridge networks do
                   not
  CLAUDE_USERNS    --userns for the claude container; unset picks keep-id under
                   rootless podman and nothing otherwise
  CLAUDE_ENV       comma-separated names of host variables forwarded into the
                   container, like -e NAME for each
  CLAUDE_HOST_PORTS comma-separated host ports reachable as localhost:PORT in
                   the container, like --host-port PORT for each
  CLAUDE_HOST_TARGET host name or address the proxy forwards those ports to
                   (default host.docker.internal or host.containers.internal,
                   mapped to the engine's host-gateway)
  CLAUDE_CONFIG_DIR host directory mounted as the Claude config
                   (default ~/.claude)
  CLAUDE_IMAGE     dev image name (default claude-dev)
  CLAUDE_TAG       dev image tag (default latest)
  CONTAINER_USER   user inside the dev image (default dev)
  PROXY_IMAGE      proxy image name (default claude-proxy)
  PROXY_TAG        proxy image tag (default latest)
  PROXY_CONTAINER  proxy container name prefix, the session id is appended
                   (default claude-proxy)
  PROXY_PORT       host port to publish the proxy on; unset publishes nothing;
                   only one session at a time can hold it
  PROXY_BIND       host address for that port (default 127.0.0.1)"""


def env(*names, default=""):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def warn(message):
    print(f"start.py: {message}", file=sys.stderr)


def note(message):
    print(message, file=sys.stderr)


def die(message):
    warn(message)
    sys.exit(1)


def query(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def succeeds(cmd):
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def must(cmd, quiet=True):
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL if quiet else None)
    if result.returncode != 0:
        sys.exit(result.returncode)


class Settings:
    def __init__(self):
        self.engine = env("CONTAINER_ENGINE", default="docker")
        if self.engine == "docker":
            self.host_internal = "host.docker.internal"
        elif self.engine == "podman":
            self.host_internal = "host.containers.internal"
        else:
            die(f"unknown container engine: {self.engine}")
        self.image = f"{env('CLAUDE_IMAGE', default='claude-dev')}:{env('CLAUDE_TAG', default='latest')}"
        self.container_user = env("CONTAINER_USER", default="dev")
        self.container_home = f"/home/{self.container_user}"
        self.session = secrets.token_hex(4)
        self.net_name = f"{env('CLAUDE_NET', default='claude-internal')}-{self.session}"
        self.bridge_net = env("CLAUDE_BRIDGE", default="claude-egress")
        self.proxy_image = f"{env('PROXY_IMAGE', default='claude-proxy')}:{env('PROXY_TAG', default='latest')}"
        self.dev_name = f"{DEV_PREFIX}-{self.session}"
        self.proxy_name = f"{env('PROXY_CONTAINER', default='claude-proxy')}-{self.session}"
        self.proxy_publish = env("PROXY_PORT")
        self.proxy_bind = env("PROXY_BIND", default="127.0.0.1")
        self.no_upstream = env("CLAUDE_NO_PROXY", "NO_PROXY", "no_proxy")
        self.upstream = ""
        self.host_gateway = False
        self.home = Path.home()
        self.config_dir = Path(env("CLAUDE_CONFIG_DIR", default=str(self.home / ".claude")))
        self.forward_env = [name.strip() for name in env("CLAUDE_ENV").split(",") if name.strip()]
        self.host_ports = []
        self.host_target = ""


def host_port(text):
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port {text!r}")
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port {port} out of range")
    if port == PROXY_LISTEN:
        raise argparse.ArgumentTypeError(f"port {port} is taken by the proxy itself")
    return port


def split_args(argv):
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            return argv[:i], argv[i + 1:]
        if arg.startswith("--"):
            name, equals, _ = arg.partition("=")
            if name in VALUE_OPTIONS:
                i += 1 if equals else 2
                continue
            if name in FLAG_OPTIONS and not equals:
                i += 1
                continue
            return argv[:i], argv[i:]
        if arg.startswith("-") and len(arg) > 1:
            j = 1
            while j < len(arg) and "-" + arg[j] in FLAG_OPTIONS:
                j += 1
            if j == len(arg):
                i += 1
                continue
            if "-" + arg[j] in VALUE_OPTIONS:
                i += 1 if j + 1 < len(arg) else 2
                continue
            return argv[:i], argv[i:]
        i += 1
    return argv, []


def parse_args(argv):
    argv, claude_args = split_args(argv)
    parser = argparse.ArgumentParser(
        prog="start.py",
        usage="%(prog)s [-s] [-e NAME[=VALUE]]... [--host-port PORT]... [PATH...] [[--] ARG...]",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("paths", nargs="*", metavar="PATH")
    parser.add_argument("-s", "--shell", action="store_true",
                        help="run bash in the container instead of claude")
    parser.add_argument("-e", "--env", action="append", default=[], metavar="NAME[=VALUE]",
                        help="set a variable in the container, or forward the host's "
                             "value when no VALUE is given; repeatable")
    parser.add_argument("--host-port", action="append", default=[], type=host_port, metavar="PORT",
                        help="make a TCP port of the host reachable as localhost:PORT "
                             "in the container; repeatable")
    options = parser.parse_args(argv)
    for item in options.env:
        if not item.partition("=")[0]:
            parser.error(f"invalid -e {item!r}: variable name is empty")
    return options, claude_args


class MountNames:
    def __init__(self):
        self.used = set()

    def assign(self, path):
        base = path.name or "root"
        name, i = base, 2
        while name in self.used:
            name = f"{base}-{i}"
            i += 1
        self.used.add(name)
        return name


def parse_proxy(url):
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*)://(.*)$", url)
    scheme, rest = (match.group(1).lower(), match.group(2)) if match else ("", url)
    rest = rest.split("/", 1)[0]
    creds, _, hostport = rest.rpartition("@")
    match = (re.match(r"^(\[[^\]]+\])(?::(\d+))?$", hostport)
             or re.match(r"^([^:]+)(?::(\d+))?$", hostport))
    if not match:
        return None
    host, port = match.group(1), match.group(2)
    if not port:
        port = {"https": "443", "http": "80"}.get(scheme, "8080")
        warn(f"no proxy port given, assuming {port}")
    if scheme == "https":
        warn("warning: tinyproxy talks to the upstream in cleartext, so an https:// proxy will not work")
    return creds, host, port


def resolve_upstream(s):
    url = env("CLAUDE_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    if not url:
        return
    parsed = parse_proxy(url)
    if not parsed:
        die(f"cannot parse proxy {url}")
    creds, host, port = parsed
    if host in HOST_ALIASES:
        host = s.host_internal
        s.host_gateway = True
    s.upstream = f"{creds}@{host}:{port}" if creds else f"{host}:{port}"


def resolve_host_ports(cli_ports):
    ports = set(cli_ports)
    for item in env("CLAUDE_HOST_PORTS").split(","):
        if item.strip():
            try:
                ports.add(host_port(item.strip()))
            except argparse.ArgumentTypeError as error:
                die(f"CLAUDE_HOST_PORTS: {error}")
    return sorted(ports)


def resolve_host_target():
    target = env("CLAUDE_HOST_TARGET").strip()
    if not target:
        return ""
    if re.match(r"^[A-Za-z0-9.\-]+$", target):
        return target
    match = re.match(r"^\[?([0-9A-Fa-f:.]+)\]?$", target)
    if match and ":" in match.group(1):
        return f"[{match.group(1)}]"
    die(f"CLAUDE_HOST_TARGET: invalid host {target!r}")


def ensure_engine(s):
    if not shutil.which(s.engine):
        die(f"{s.engine} is not installed or not on PATH")
    if not succeeds([s.engine, "info"]):
        die(f"cannot talk to the {s.engine} engine; is it running?")


def network_field(s, name, field):
    return query([s.engine, "network", "inspect", "-f", "{{" + field + "}}", name])


def create_session_network(s):
    must([s.engine, "network", "create", "--internal",
          "--label", "claude.role=session",
          "--label", f"claude.session={s.session}",
          "--label", f"claude.created={int(time.time())}",
          s.net_name])


def ensure_bridge_network(s):
    internal = network_field(s, s.bridge_net, ".Internal")
    if internal is not None:
        if internal == "true":
            die(f"network {s.bridge_net} is internal; the proxy cannot reach the outside through it")
        if s.engine == "podman" and network_field(s, s.bridge_net, ".DNSEnabled") == "false":
            warn(f"warning: network {s.bridge_net} serves no DNS, the proxy will not resolve host names")
        return
    must([s.engine, "network", "create", s.bridge_net])
    note(f"created outward network {s.bridge_net}")


def wait_proxy(s):
    probe = f"http_proxy=http://127.0.0.1:{PROXY_LISTEN} wget -q -O /dev/null http://tinyproxy.stats/"
    for _ in range(20):
        if succeeds([s.engine, "exec", s.proxy_name, "sh", "-c", probe]):
            return
        time.sleep(0.25)
    die(f"proxy {s.proxy_name} is not answering on port {PROXY_LISTEN}; see {s.engine} logs {s.proxy_name}")


def ensure_proxy_image(s):
    if not succeeds([s.engine, "image", "inspect", s.proxy_image]):
        build_env = dict(os.environ, CONTAINER_ENGINE=s.engine)
        result = subprocess.run([sys.executable, str(SCRIPT_DIR / "build.py"), "proxy"], env=build_env)
        if result.returncode != 0:
            sys.exit(result.returncode)


def quietly(cmd):
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def session_gone(s, session):
    return bool(session) and not succeeds([s.engine, "inspect", f"{DEV_PREFIX}-{session}"])


def remove_orphans(s):
    ids = (query([s.engine, "ps", "-aq", "--filter", "label=claude.role=proxy"]) or "").split()
    for container in ids:
        session = query([s.engine, "inspect", "-f", '{{index .Config.Labels "claude.session"}}', container])
        if session_gone(s, session):
            quietly([s.engine, "rm", "-f", container])
            note(f"removed proxy {container} left over from session {session}")

    ids = (query([s.engine, "network", "ls", "-q", "--filter", "label=claude.role=session"]) or "").split()
    for network in ids:
        labels = query([s.engine, "network", "inspect", "-f",
                        '{{index .Labels "claude.session"}}|{{index .Labels "claude.created"}}', network])
        session, _, created = (labels or "").partition("|")
        if not created.isdigit() or time.time() - int(created) < ORPHAN_GRACE:
            continue
        if session_gone(s, session):
            quietly([s.engine, "network", "rm", network])
            note(f"removed network {network} left over from session {session}")


def start_proxy(s):
    host_ports = ",".join(str(port) for port in s.host_ports)
    args = [
        "--name", s.proxy_name,
        "--rm",
        "--network", s.bridge_net,
        "--label", "claude.role=proxy",
        "--label", f"claude.session={s.session}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
    ]
    if s.upstream:
        args += ["-e", f"UPSTREAM_PROXY={s.upstream}"]
    if s.no_upstream:
        args += ["-e", f"NO_UPSTREAM={s.no_upstream}"]
    if host_ports:
        args += ["-e", f"HOST_PORTS={host_ports}", "-e", f"HOST_TARGET={s.host_target or s.host_internal}"]
    if s.host_gateway or (host_ports and not s.host_target):
        args += ["--add-host", f"{s.host_internal}:host-gateway"]
    if s.proxy_publish:
        args += ["-p", f"{s.proxy_bind}:{s.proxy_publish}:{PROXY_LISTEN}"]

    # Created on the bridge, joined to the session network, then started: tinyproxy
    # must never come up on a container that cannot yet resolve its upstream, and
    # host-gateway must resolve on the bridge, not on the internal network.
    must([s.engine, "create", *args, s.proxy_image])
    must([s.engine, "network", "connect", "--alias", "proxy", s.net_name, s.proxy_name], quiet=False)
    must([s.engine, "start", s.proxy_name])
    wait_proxy(s)


def remove_session(s):
    for name in ("SIGHUP", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_IGN)
    quietly([s.engine, "rm", "-f", s.dev_name, s.proxy_name])
    quietly([s.engine, "network", "rm", s.net_name])


def merge_mcp_servers(host_file, contained_file):
    if not (host_file.is_file() and contained_file.is_file()):
        return
    if host_file.samefile(contained_file):
        return
    try:
        host = json.loads(host_file.read_text(encoding="utf-8"))
        contained = json.loads(contained_file.read_text(encoding="utf-8"))
        servers = dict(contained.get("mcpServers") or {})
        servers.update(host.get("mcpServers") or {})
        contained["mcpServers"] = servers
        merged = json.dumps(contained, indent=2)
    except (OSError, ValueError, AttributeError, TypeError) as error:
        warn(f"merging mcpServers from {host_file} failed; leaving {contained_file} unchanged: {error}")
        return
    fd, tmp = tempfile.mkstemp(prefix=contained_file.name + ".", dir=str(contained_file.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(merged)
        try:
            shutil.copymode(str(contained_file), tmp)
        except OSError:
            pass
        os.replace(tmp, str(contained_file))
    except OSError as error:
        if os.path.exists(tmp):
            os.unlink(tmp)
        warn(f"merging mcpServers from {host_file} failed; leaving {contained_file} unchanged: {error}")


def prepare_config(s):
    s.config_dir.mkdir(parents=True, exist_ok=True)
    host_config = s.home / ".claude.json"
    contained_config = s.config_dir / ".claude.json"
    if not contained_config.is_file() and host_config.is_file():
        shutil.copyfile(str(host_config), str(contained_config))
    merge_mcp_servers(host_config, contained_config)


def resolve_userns(s):
    # Rootless podman maps the invoking user to root inside, which would leave the
    # bind-mounted workspace owned by the wrong uid for the container user.
    if "CLAUDE_USERNS" in os.environ:
        return os.environ["CLAUDE_USERNS"]
    if s.engine == "podman" and query([s.engine, "info", "-f", "{{.Host.Security.Rootless}}"]) == "true":
        return "keep-id"
    return ""


def run_args(s, paths, extra_env):
    in_proxy = f"http://proxy:{PROXY_LISTEN}"
    no_proxy = "localhost,127.0.0.1,::1,proxy"
    args = [
        "--name", s.dev_name,
        "--rm",
        "--label", "claude.role=dev",
        "--label", f"claude.session={s.session}",
        "--network", s.net_name,
        "--cap-drop", "NET_ADMIN",
        "--cap-drop", "NET_RAW",
        "--security-opt", "no-new-privileges",
        "-v", f"{s.config_dir}:{s.container_home}/.claude",
        "-e", f"CLAUDE_CONFIG_DIR={s.container_home}/.claude",
    ]
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        args += ["-e", f"{name}={in_proxy}"]
    for name in ("NO_PROXY", "no_proxy"):
        args += ["-e", f"{name}={no_proxy}"]
    if s.host_ports:
        args += ["-e", "CLAUDE_HOST_PORTS=" + ",".join(str(port) for port in s.host_ports)]

    userns = resolve_userns(s)
    if userns:
        args += ["--userns", userns]

    names = MountNames()
    cwd = Path.cwd().resolve()
    workdir_name = names.assign(cwd)
    args += ["-v", f"{cwd}:/workspace/{workdir_name}"]
    note(f"mount {cwd} -> /workspace/{workdir_name} (workdir)")

    for raw in paths:
        try:
            path = Path(raw).expanduser().resolve(strict=True)
        except OSError:
            die(f"no such path: {raw}")
        name = names.assign(path)
        args += ["-v", f"{path}:/workspace/{name}"]
        note(f"mount {path} -> /workspace/{name}")

    for port in s.host_ports:
        note(f"port localhost:{port} -> {s.host_target or s.host_internal}:{port}")

    args += ["-w", f"/workspace/{workdir_name}"]

    # The terminal type and COLORTERM decide what the programs inside are willing to
    # emit; without them TERM falls back to plain xterm and 24-bit color is dropped.
    # Windows consoles set neither, so name what Windows Terminal and modern conhost
    # actually support. The image carries ncurses-term, so exotic entries like
    # xterm-kitty resolve.
    if sys.stdin.isatty():
        args.append("-it")
        if os.name == "nt":
            args += ["-e", f"TERM={env('TERM', default='xterm-256color')}",
                     "-e", f"COLORTERM={env('COLORTERM', default='truecolor')}"]
            terminal_vars = ("TERM_PROGRAM", "TERM_PROGRAM_VERSION")
        else:
            terminal_vars = ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION")
        for name in terminal_vars:
            if os.environ.get(name):
                args += ["-e", name]

    gitconfig = s.home / ".gitconfig"
    if gitconfig.is_file():
        args += ["-v", f"{gitconfig}:{s.container_home}/.gitconfig:ro"]

    if shutil.which("git"):
        autocrlf = query(["git", "-C", str(cwd), "config", "--get", "core.autocrlf"])
        if autocrlf:
            args += ["-e", "GIT_CONFIG_COUNT=1",
                     "-e", "GIT_CONFIG_KEY_0=core.autocrlf",
                     "-e", f"GIT_CONFIG_VALUE_0={autocrlf}"]

    for name in PASSTHROUGH_VARS:
        if os.environ.get(name):
            args += ["-e", name]

    for item in [*s.forward_env, *extra_env]:
        args += ["-e", item]
    return args


def exit_on_signal(signum, _frame):
    raise SystemExit(128 + signum)


def run_session(s, create_cmd):
    for name in ("SIGHUP", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), exit_on_signal)
    try:
        create_session_network(s)
        must(create_cmd)
        start_proxy(s)
        signal.signal(signal.SIGINT, lambda *_: None)
        attach = "-ai" if sys.stdin.isatty() else "-a"
        return subprocess.call([s.engine, "start", attach, s.dev_name])
    finally:
        remove_session(s)


def main():
    options, claude_args = parse_args(sys.argv[1:])
    s = Settings()
    s.host_ports = resolve_host_ports(options.host_port)
    s.host_target = resolve_host_target()
    ensure_engine(s)
    if not succeeds([s.engine, "image", "inspect", s.image]):
        die(f"image {s.image} is missing; run {SCRIPT_DIR / 'build.py'}")
    prepare_config(s)
    resolve_upstream(s)
    ensure_bridge_network(s)
    remove_orphans(s)
    ensure_proxy_image(s)
    args = run_args(s, options.paths, options.env)
    program = "bash" if options.shell else "claude"
    sys.exit(run_session(s, [s.engine, "create", *args, s.image, program, *claude_args]))


if __name__ == "__main__":
    main()
