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

from hostexec import detect as hostexec_detect
from hostexec import relaylink
from hostexec import server as hostexec_server

SCRIPT_DIR = Path(__file__).resolve().parent
PROXY_LISTEN = 3128
HOSTEXEC_MOUNT = "/opt/hostexec"
HOSTEXEC_SOCKET = "/tmp/hostexec/sock"
HOSTEXEC_DIR = SCRIPT_DIR / "hostexec" / "container"
HOST_ALIASES = {
    "localhost", "127.0.0.1", "::1", "[::1]",
    "host.docker.internal", "host.containers.internal",
}
PASSTHROUGH_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
)

DESCRIPTION = """\
Ensures the egress proxy is running, then starts claude in a container. The
current directory is always mounted and used as the working directory. Each
additional PATH is mounted read-write under /workspace, named after the last
component of that path.

Anything after -- is passed on to claude itself, or to bash with -s.

Environment variables reach the container only when named: -e NAME forwards
the host's value (nothing is set when it is unset on the host), -e NAME=VALUE
sets one explicitly. CLAUDE_ENV names host variables to forward on every start.

Networking
  The claude container runs on an internal container network with no route off
  the host. Its only way out is the tinyproxy container, which sits on that
  network as http://proxy:3128 and on a second, outward-facing network. That
  proxy goes out over the host connection, or forwards to the corporate proxy
  named by CLAUDE_PROXY, except for the hosts named by CLAUDE_NO_PROXY. A proxy
  container left over from an earlier start is reused, and recreated when its
  upstream, that list or either network differs from the one in the current
  environment.

  The proxy image is built on demand; the dev image has to be built beforehand
  with build.py.

Host exec
  With --host-exec (or CLAUDE_HOST_EXEC=1) the working directory is scanned for
  a known build system (package.json, a .NET solution or project, a Makefile
  with build/test targets, pyproject.toml) and a small server in this process
  offers the two verbs build and test to the container. Inside, `hostrun build`
  and `hostrun test` run the host's toolchain in the working directory and
  relay output and exit status; a skill tells claude about them. The host
  reaches the container through a single `exec` session, so no port, mount or
  network change is involved. Note that this runs whatever the repo's build
  definition says with your rights on the host, and claude can edit that
  definition."""

EPILOG = """\
environment:
  CONTAINER_ENGINE container frontend, docker or podman (default docker)
  CLAUDE_PROXY     corporate proxy the egress forwards to, e.g.
                   http://proxy.corp:3128 or http://user:pass@host:port; falls
                   back to HTTPS_PROXY/HTTP_PROXY, unset means direct
  CLAUDE_NO_PROXY  comma-separated hosts, domains (.corp.example) and networks
                   (10.0.0.0/8) the proxy reaches directly instead of through
                   the upstream; falls back to NO_PROXY
  CLAUDE_NET       internal network name (default claude-internal)
  CLAUDE_BRIDGE    outward-facing network the proxy reaches the outside on,
                   created when missing (default claude-egress); it has to
                   carry DNS, which the engines' predefined bridge networks do
                   not
  CLAUDE_USERNS    --userns for the claude container; unset picks keep-id under
                   rootless podman and nothing otherwise
  CLAUDE_ENV       comma-separated names of host variables forwarded into the
                   container, like -e NAME for each
  CLAUDE_CONFIG_DIR host directory mounted as the Claude config
                   (default ~/.claude)
  CLAUDE_HOST_EXEC 1 enables host exec, same as --host-exec
  CLAUDE_HOST_MODULE force the host exec module (npm, dotnet, make, python)
                   instead of detecting one
  CLAUDE_IMAGE     dev image name (default claude-dev)
  CLAUDE_TAG       dev image tag (default latest)
  CONTAINER_USER   user inside the dev image (default dev)
  PROXY_IMAGE      proxy image name (default claude-proxy)
  PROXY_TAG        proxy image tag (default latest)
  PROXY_CONTAINER  proxy container name (default claude-proxy)
  PROXY_PORT       host port to publish the proxy on; unset publishes nothing
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
        self.net_name = env("CLAUDE_NET", default="claude-internal")
        self.bridge_net = env("CLAUDE_BRIDGE", default="claude-egress")
        self.proxy_image = f"{env('PROXY_IMAGE', default='claude-proxy')}:{env('PROXY_TAG', default='latest')}"
        self.proxy_name = env("PROXY_CONTAINER", default="claude-proxy")
        self.proxy_publish = env("PROXY_PORT")
        self.proxy_bind = env("PROXY_BIND", default="127.0.0.1")
        self.no_upstream = env("CLAUDE_NO_PROXY", "NO_PROXY", "no_proxy")
        self.upstream = ""
        self.host_gateway = False
        self.home = Path.home()
        self.config_dir = Path(env("CLAUDE_CONFIG_DIR", default=str(self.home / ".claude")))
        self.host_exec = env("CLAUDE_HOST_EXEC").lower() in ("1", "true", "yes", "on")
        self.forward_env = [name.strip() for name in env("CLAUDE_ENV").split(",") if name.strip()]
        self.container_name = ""


def parse_args(argv):
    claude_args = []
    if "--" in argv:
        split = argv.index("--")
        argv, claude_args = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(
        prog="start.py",
        usage="%(prog)s [--host-exec] [-s] [-e NAME[=VALUE]]... [PATH...] [-- CLAUDE_ARG...]",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", metavar="PATH")
    parser.add_argument("--host-exec", action="store_true",
                        help="let claude run the repo's build and test on the host")
    parser.add_argument("-s", "--shell", action="store_true",
                        help="run bash in the container instead of claude")
    parser.add_argument("-e", "--env", action="append", default=[], metavar="NAME[=VALUE]",
                        help="set a variable in the container, or forward the host's "
                             "value when no VALUE is given; repeatable")
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


def ensure_engine(s):
    if not shutil.which(s.engine):
        die(f"{s.engine} is not installed or not on PATH")
    if not succeeds([s.engine, "info"]):
        die(f"cannot talk to the {s.engine} engine; is it running?")


def network_field(s, name, field):
    return query([s.engine, "network", "inspect", "-f", "{{" + field + "}}", name])


def ensure_network(s):
    internal = network_field(s, s.net_name, ".Internal")
    if internal is not None:
        if internal != "true":
            die(f"network {s.net_name} exists but is not internal; remove it or set CLAUDE_NET")
        return
    must([s.engine, "network", "create", "--internal", s.net_name])
    note(f"created internal network {s.net_name}")


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


def ensure_proxy(s):
    labels = "|".join(f'{{{{index .Config.Labels "claude.{key}"}}}}'
                      for key in ("upstream", "noupstream", "network", "bridge"))
    current = query([s.engine, "inspect", "-f", labels, s.proxy_name]) or ""
    wanted = f"{s.upstream}|{s.no_upstream}|{s.net_name}|{s.bridge_net}"
    if current == wanted:
        if query([s.engine, "inspect", "-f", "{{.State.Running}}", s.proxy_name]) != "true":
            must([s.engine, "start", s.proxy_name])
        wait_proxy(s)
        return
    if current:
        note(f"proxy settings changed, recreating {s.proxy_name}")
    if not succeeds([s.engine, "image", "inspect", s.proxy_image]):
        build_env = dict(os.environ, CONTAINER_ENGINE=s.engine)
        result = subprocess.run([sys.executable, str(SCRIPT_DIR / "build.py"), "proxy"], env=build_env)
        if result.returncode != 0:
            sys.exit(result.returncode)
    subprocess.run([s.engine, "rm", "-f", s.proxy_name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    args = [
        "--name", s.proxy_name,
        "--restart", "unless-stopped",
        "--network", s.net_name,
        "--network-alias", "proxy",
        "--label", f"claude.upstream={s.upstream}",
        "--label", f"claude.noupstream={s.no_upstream}",
        "--label", f"claude.network={s.net_name}",
        "--label", f"claude.bridge={s.bridge_net}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
    ]
    if s.upstream:
        args += ["-e", f"UPSTREAM_PROXY={s.upstream}"]
    if s.no_upstream:
        args += ["-e", f"NO_UPSTREAM={s.no_upstream}"]
    if s.host_gateway:
        args += ["--add-host", f"{s.host_internal}:host-gateway"]
    if s.proxy_publish:
        args += ["-p", f"{s.proxy_bind}:{s.proxy_publish}:{PROXY_LISTEN}"]

    # Created, bridged, then started: tinyproxy must never come up on a container
    # that cannot yet resolve its upstream.
    must([s.engine, "create", *args, s.proxy_image])
    must([s.engine, "network", "connect", s.bridge_net, s.proxy_name], quiet=False)
    must([s.engine, "start", s.proxy_name])
    wait_proxy(s)
    described = s.upstream.rpartition("@")[2] if s.upstream else "direct"
    note(f"proxy {s.proxy_name} serving {s.net_name} -> {described}")


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
        "--rm",
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


def prepare_host_exec(s):
    cwd = Path.cwd().resolve()
    try:
        module = hostexec_detect.pick(cwd)
    except LookupError as error:
        die(str(error))
    if module is None:
        warn(f"host exec: no known build system in {cwd}, feature disabled")
        return None
    offered = module.describe(cwd)
    if not offered:
        warn(f"host exec: module {module.NAME} offers nothing in {cwd}, feature disabled")
        return None
    described = ", ".join(f"{verb}: {command}" for verb, command in sorted(offered.items()))
    note(f"host exec: {module.NAME} ({described})")
    s.container_name = f"claude-dev-{secrets.token_hex(3)}"
    return hostexec_server.Server(module, cwd, s.config_dir / "hostexec.log")


def host_exec_args(s):
    return [
        "--name", s.container_name,
        "-e", f"CLAUDE_HOST_EXEC={HOSTEXEC_SOCKET}",
        "-v", f"{HOSTEXEC_DIR}:{HOSTEXEC_MOUNT}:ro",
    ]


def container_running(s):
    return query([s.engine, "inspect", "-f", "{{.State.Running}}", s.container_name]) == "true"


def wait_container(s, proc):
    for _ in range(120):
        if container_running(s):
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def exec_claude(cmd):
    if os.name != "nt":
        os.execvp(cmd[0], cmd)
    signal.signal(signal.SIGINT, lambda *_: None)
    sys.exit(subprocess.call(cmd))


def run_claude_with_host_exec(s, cmd, server):
    signal.signal(signal.SIGINT, lambda *_: None)
    server.start()
    proc = subprocess.Popen(cmd)
    link = None
    try:
        if wait_container(s, proc):
            link = relaylink.Link(
                relaylink.exec_command(s.engine, s.container_name, HOSTEXEC_MOUNT),
                server, server.log, alive=lambda: container_running(s))
            link.start()
        elif proc.poll() is None:
            warn(f"host exec: container {s.container_name} did not come up in time, feature disabled")
        return proc.wait()
    finally:
        if link is not None:
            link.stop()
        server.stop()


def main():
    options, claude_args = parse_args(sys.argv[1:])
    s = Settings()
    s.host_exec = s.host_exec or options.host_exec
    ensure_engine(s)
    if not succeeds([s.engine, "image", "inspect", s.image]):
        die(f"image {s.image} is missing; run {SCRIPT_DIR / 'build.py'}")
    prepare_config(s)
    resolve_upstream(s)
    ensure_network(s)
    ensure_bridge_network(s)
    ensure_proxy(s)
    args = run_args(s, options.paths, options.env)
    program = "bash" if options.shell else "claude"
    server = prepare_host_exec(s) if s.host_exec else None
    if server is None:
        exec_claude([s.engine, "run", *args, s.image, program, *claude_args])
    args += host_exec_args(s)
    if not options.shell:
        claude_args = ["--plugin-dir", f"{HOSTEXEC_MOUNT}/plugin", *claude_args]
    sys.exit(run_claude_with_host_exec(s, [s.engine, "run", *args, s.image, program, *claude_args], server))


if __name__ == "__main__":
    main()
