#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROXY_LISTEN = 3128
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

Anything after -- is passed on to claude itself.

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
  with build.py."""

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
  CLAUDE_CONFIG_DIR host directory mounted as the Claude config
                   (default ~/.claude)
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


def parse_args(argv):
    claude_args = []
    if "--" in argv:
        split = argv.index("--")
        argv, claude_args = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(
        prog="start.py",
        usage="%(prog)s [PATH...] [-- CLAUDE_ARG...]",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", metavar="PATH")
    return parser.parse_args(argv).paths, claude_args


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


def run_args(s, paths):
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

    for name in PASSTHROUGH_VARS:
        if os.environ.get(name):
            args += ["-e", name]
    return args


def exec_claude(cmd):
    if os.name != "nt":
        os.execvp(cmd[0], cmd)
    signal.signal(signal.SIGINT, lambda *_: None)
    sys.exit(subprocess.call(cmd))


def main():
    paths, claude_args = parse_args(sys.argv[1:])
    s = Settings()
    ensure_engine(s)
    if not succeeds([s.engine, "image", "inspect", s.image]):
        die(f"image {s.image} is missing; run {SCRIPT_DIR / 'build.py'}")
    prepare_config(s)
    resolve_upstream(s)
    ensure_network(s)
    ensure_bridge_network(s)
    ensure_proxy(s)
    args = run_args(s, paths)
    exec_claude([s.engine, "run", *args, s.image, "claude", *claude_args])


if __name__ == "__main__":
    main()
