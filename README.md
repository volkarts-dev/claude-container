# claude-container

A sandbox for running [Claude Code](https://claude.com/claude-code) in a container that
cannot reach the network on its own. Claude gets a full development toolchain and the
directories you explicitly mount — nothing else. All traffic leaving the sandbox goes
through an HTTP proxy you control, which can be pointed at a corporate proxy or sent
straight out over the host connection.

## Architecture

Two containers on two networks:

```
                              internal network                             outward network
                              (claude-internal-<id>)                       (claude-egress)
                              no route off host
  ┌───────────────────────┐                     ┌──────────────────────┐
  │  claude-dev           │                     │   claude-proxy       │
  │  ───────────────────  │   http://proxy:3128 │   ─────────────────  │
  │  claude code          ├────────────────────►│   tinyproxy          ├──► upstream
  │  node / .NET / git    │                     │                      │    proxy or
  │  pwsh, ripgrep, jq, … │                     │                      │    direct
  │                       │                     │                      │    egress
  │  --cap-drop NET_ADMIN │                     │   --cap-drop ALL     │
  │  --cap-drop NET_RAW   │                     │   no-new-privileges  │
  │  no-new-privileges    │                     │                      │
  │  no setuid, no su     │                     │                      │
  │  [per session]        │                     │   [per session]      │
  └───────┬───────────────┘                     └──────────────────────┘
          │ bind mounts
          │
   /workspace/<name>      ← the current directory (workdir) plus any extra paths
   ~/.claude              ← host config directory, read-write, persists sessions
   ~/.gitconfig           ← read-only
```

**The dev container** (`claude/Dockerfile`) is Debian 13 slim with Node.js (from the
official nodejs.org tarball, checksum verified), the .NET SDK, PowerShell (from the
official GitHub release tarball, checksum verified), `git`, `git-lfs`, `ripgrep`,
`fd-find`, `jq`, build and test tooling (including CMake, Ninja, ccache, `clang` and
`clang-format` from the Debian archive, the GCC and clang sanitizer runtimes, Playwright
with Chromium and the X11/Wayland/OpenGL/Vulkan and FreeType development headers needed
to build windowing and graphics libraries such as GLFW), Python 3 and the usual shell
utilities. The Claude Code CLI is installed globally with npm. It runs as an
unprivileged user (`dev` by default) created with the *host's* uid/gid, so files written
into the mounted workspace keep the right ownership. `tini` is the entrypoint so signals
and zombie processes behave; under it `entrypoint.sh` starts an Xvfb virtual X server on
`DISPLAY=:99` (pick another with `-e DISPLAY=:N`) before running claude or bash, so GUI
programs and tests run headless without `xvfb-run`. The locale is `en_US.UTF-8` and the
timezone `Europe/Berlin`; another zone can be picked per start with `-e TZ=Area/City`.

There is no way to become root inside it: `sudo` is not installed, `su` and the other
switch-user tools are deleted, every setuid/setgid bit in the image is cleared at build
time, and the container runs with `no-new-privileges`, which makes the kernel refuse any
privilege gain from an exec regardless.

**The proxy container** (`proxy/`) is tinyproxy. Its config is generated at start time
by `entrypoint.sh` from the `UPSTREAM_PROXY` and `NO_UPSTREAM` environment variables, so
switching networks never requires a rebuild. `NO_UPSTREAM` entries become `Upstream none`
rules (both bare and dot-prefixed forms of a domain, CIDR blocks passed through), and
`UPSTREAM_PROXY` becomes the catch-all `Upstream http` line. Access is limited to the
private RFC1918/ULA ranges. `CONNECT` is limited to port 443 (`ConnectPort`), and plain
HTTP to port 80 by the URL filter in `proxy/filter` (`FilterURLs`, `FilterDefaultDeny`):
only `http://` URLs without a port or with `:80`, and `CONNECT` targets on `:443`, match
it; everything else gets a `403 Filtered`. Next to tinyproxy the
container runs one `socat` listener per [host port](#reaching-services-on-the-host), which
hands each connection over to `start.py` through the container engine and opens no
connection of its own.

**The two networks** exist because of DNS. The dev container sits only on an `--internal`
network of its own, which has no route off the host — the proxy is its single exit, and
no other session's containers are on it. The proxy also
joins a normal user-defined bridge network, because the engines' *predefined* bridge
networks serve no DNS and an internal network's resolver refuses to forward, which would 
leave the proxy unable to resolve either its upstream or the sites it is asked to fetch.

**The start script** (`start.py`) wires all of this up on every launch: it
resolves the upstream proxy, creates the outward network if missing, creates the
session's internal network and the dev container on it with the mounts, proxy variables
and terminal settings in place, starts a proxy container of its own next to it, waits
until tinyproxy actually answers, then attaches to the dev container. The network and
both containers carry a random session id in their names (`claude-internal-<id>`,
`claude-dev-<id>`, `claude-proxy-<id>`) and in a `claude.session` label, so sessions run
side by side without seeing each other. When the session ends — claude exits, or the
script gets `SIGHUP`/`SIGTERM` — both containers and the network are removed. A proxy
or network whose dev container is gone, because the script was killed outright, is
removed on the next start; networks only once they are a minute old, since a network
briefly exists before its dev container does.

**The build script** (`build.py`) builds the two images. With `--update` it pulls the
latest base images and rebuilds from scratch, ignoring the layer cache.

Both scripts are plain Python with no third-party dependencies and run the same way on
Linux, macOS and Windows. `start.sh` and `start.ps1` are thin wrappers that run
`start.py` with the same arguments, for shells where the interpreter is not on the
path lookup or a file association is more convenient.

## Prerequisites

- Python 3.8 or newer on the host. On Linux and macOS the scripts run directly
  (`./build.py`); on Windows call them through the interpreter (`python build.py`).
- Docker or podman, with the engine running.

## Getting started

Build the images once:

```sh
./build.py                 # both images
./build.py [claude|proxy]  # just the dev or proxy image
./build.py --update        # pull new base images and rebuild from scratch
```

Then start Claude from whatever project you want to work on:

```sh
cd ~/projects/my-app
/path/to/claude-container/start.py      # or start.sh / start.ps1
```

The current directory is mounted at `/workspace/my-app` and becomes the working
directory. The proxy image is built on demand; the dev image is not, so `build.py` has to
have run first.

## Usage

Mount extra directories alongside the working directory — each lands under `/workspace`
named after its last path component, with a `-2`, `-3` suffix on collisions:

```sh
./start.py ../shared-lib ~/data
```

Options the start script does not know are passed through to `claude` itself, or to
`bash` with `-s`. The first unknown option and everything after it go to the program,
so the script's own options and the paths to mount have to come before it:

```sh
./start.py --resume
./start.py ../shared-lib --model opus
./start.py -s -c 'dotnet --info'
```

Everything after `--` is passed through as well. That is needed for arguments that do
not start with a dash, like a prompt, or that the script would take for its own, like
`-h`:

```sh
./start.py -- "explain this repository"
./start.py -- --help
```

```powershell
./start.ps1 ..\shared-lib --model opus
./start.ps1 '--' "explain this repository"
./start.ps1 --% -- "explain this repository"
```

Inside a PowerShell session a bare `--` is consumed by PowerShell itself before the
wrapper sees it, so quote it or put the stop-parsing token `--%` in front of it.
`pwsh -File start.ps1 ... -- ...` from another shell needs neither.

Pass environment variables into the container with `-e`, repeatable. `-e NAME` forwards
the host's value (and sets nothing when it is unset there), `-e NAME=VALUE` sets it
explicitly. Variables you always want forwarded can be listed by name in `CLAUDE_ENV`:

```sh
./start.py -e GITHUB_TOKEN -e DOTNET_CLI_TELEMETRY_OPTOUT=1
CLAUDE_ENV=GITHUB_TOKEN,NUGET_API_KEY ./start.py
```

Use podman instead of docker:

```sh
CONTAINER_ENGINE=podman ./start.py
```

```powershell
$env:CONTAINER_ENGINE="podman"
python start.py
```

Under rootless podman the script defaults to `--userns keep-id` so the bind-mounted
workspace stays owned by your uid.

### Reaching services on the host

A server running on the host — a Vitest API or browser server, a Vite dev server, a local
backend — can be made reachable from inside the container with `--host-port`, repeatable,
or the comma-separated `CLAUDE_HOST_PORTS`:

```sh
./start.py --host-port 51204 --host-port 5173
CLAUDE_HOST_PORTS=51204,5173 ./start.py
```

Inside the container the port answers on `localhost:PORT` (both `127.0.0.1` and `::1`),
so URLs and configs work unchanged, and dev servers that check the `Host` header, like
Vite's `server.allowedHosts`, see `localhost`. On the host the connection arrives at
`127.0.0.1:PORT`, so the server can stay bound to `localhost`. The path is plain TCP, so
WebSockets and anything else on that port pass through:

```
claude-dev-<id>               claude-proxy-<id>                         host
localhost:PORT ── socat ────► proxy:PORT ── socat ── engine exec ─────► start.py ──► 127.0.0.1:PORT
```

The last hop is not a network connection. For every connection the proxy accepts, it asks
`start.py` for a channel; `start.py` opens one with `docker exec -i` (or `podman exec -i`)
into the proxy container and connects to `127.0.0.1:PORT` itself. The containers get no
route to the host for this: no `host-gateway`, no listener on the host, no firewall
rule. It works the same with Docker Desktop, a Linux engine, rootless podman, a podman
machine and an engine inside WSL2 in NAT mode, where the containers cannot reach Windows'
`localhost` otherwise.

"The host" is the machine `start.py` runs on. Under WSL2, run it where the server runs:
`start.ps1` or `python start.py` on Windows for a server on Windows, with a podman
machine, Docker Desktop or a docker CLI that talks to the engine in WSL; `./start.py`
inside the WSL distro for a server running there. In NAT mode a `start.py` inside WSL
cannot reach a server bound to Windows' `localhost`.

`CLAUDE_HOST_TARGET` names another address for `start.py` to connect to instead of
`127.0.0.1`: `::1` for a server that listens on the IPv6 loopback only, or any other
address the host can reach:

```sh
CLAUDE_HOST_TARGET=::1 ./start.py --host-port 5173
```

```powershell
$env:CLAUDE_HOST_TARGET="::1"
python start.py --host-port 5173
```

Only the listed ports are forwarded, and only to that address; tinyproxy and its port
80/443 limits are untouched. The proxy's listeners accept connections from the session's
internal network only, so other containers on the outward network cannot use them.

Each new connection waits for the engine to start an exec session; after that it is a
plain byte stream. Up to 64 connections are relayed at a time, further ones wait for a
free slot. A connection to a port nothing listens on at the host is closed right away.

Vitest's API server can stay on its default `localhost` binding this way. Bound to any
other host, Vitest switches `api.allowWrite` and `api.allowExec` off unless they are set
explicitly, which disables writing files and running tests through the API.

Each session has its own proxy, so sessions can forward different ports. Two sessions
forwarding the same port both reach the same service on the host.

### Behind a corporate proxy

```sh
CLAUDE_PROXY=http://proxy.corp:3128 \
CLAUDE_NO_PROXY=.corp.example,10.0.0.0/8 \
./start.py
```

`CLAUDE_PROXY` falls back to `HTTPS_PROXY`/`HTTP_PROXY` and `CLAUDE_NO_PROXY` to
`NO_PROXY`, so an already-configured shell usually just works. A proxy on `localhost` is
rewritten to the host gateway automatically. Note that tinyproxy talks to its upstream in
cleartext — an `https://` proxy URL will not work, and the script warns about it.

### What gets carried into the container

- `~/.claude` (or `$CLAUDE_CONFIG_DIR`) is mounted read-write and `CLAUDE_CONFIG_DIR`
  points at it, so credentials, settings and session history survive. `.claude.json` is
  kept *inside* that directory — mounting it as a single file would break on Claude
  Code's atomic rewrite — and is seeded from `~/.claude.json` on first run. On every
  run the user-scope `mcpServers` from `~/.claude.json` are merged into it (host
  entries win on the same name; servers added inside the container are kept), so
  `claude mcp add -s user` on the host shows up in the container. Project-scoped
  servers are keyed by absolute path and will not match the `/workspace` mount; use a
  `.mcp.json` in the project for those.
- `~/.gitconfig` read-only, if present.
- `core.autocrlf` as the host's git resolves it for the working directory, passed as
  `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`. Git for Windows keeps its
  default in the system config, which is not mounted, so without this the container's git
  would report every CRLF file in a Windows checkout as modified. Unset on the host means
  nothing is passed.
- `TERM`, `COLORTERM`, `TERM_PROGRAM`, `TERM_PROGRAM_VERSION`, so colours and key
  handling match your terminal. On Windows `start.py` defaults to
  `xterm-256color`/`truecolor` since Windows consoles set neither.
- `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
  `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` — only when set on the host.

Everything else is left outside. The dev container itself is `--rm`: nothing written
outside the mounts survives the session.

## Configuration

Both scripts are configured through environment variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CONTAINER_ENGINE` | `docker` | Container frontend, `docker` or `podman` |
| `CLAUDE_IMAGE` / `CLAUDE_TAG` | `claude-dev` / `latest` | Dev image name and tag |
| `CONTAINER_USER` | `dev` | User inside the dev image |
| `NODE_MAJOR` | `22` | Node.js major version (build only) |
| `DOTNET_CHANNEL` | `10.0` | .NET channel (build only) |
| `CLAUDE_PROXY` | `HTTPS_PROXY`/`HTTP_PROXY` | Upstream proxy the egress forwards to; unset means direct |
| `CLAUDE_NO_PROXY` | `NO_PROXY` | Hosts, domains (`.corp.example`) and networks (`10.0.0.0/8`) reached without the upstream |
| `CLAUDE_NET` | `claude-internal` | Internal network name prefix; the session id is appended |
| `CLAUDE_BRIDGE` | `claude-egress` | Outward-facing network; must carry DNS |
| `CLAUDE_USERNS` | auto | `--userns` for the dev container |
| `CLAUDE_ENV` | unset | Comma-separated names of host variables forwarded into the dev container |
| `CLAUDE_HOST_PORTS` | unset | Comma-separated host ports reachable as `localhost:PORT` in the dev container |
| `CLAUDE_HOST_TARGET` | `127.0.0.1` | Host name or address `start.py` forwards the host ports to |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Host directory mounted as the Claude config |
| `PROXY_IMAGE` / `PROXY_TAG` | `claude-proxy` / `latest` | Proxy image name and tag |
| `PROXY_CONTAINER` | `claude-proxy` | Proxy container name prefix; the session id is appended |
| `PROXY_PORT` | unset | Host port to publish the proxy on; only one session at a time can hold it |
| `PROXY_BIND` | `127.0.0.1` | Host address for that port |

## Notes and limits

- The proxy container lives exactly as long as its session, so every start pays for
  starting tinyproxy. A proxy image built before host ports were handed over through
  the engine does not start with `--host-port`; rebuild it with `./build.py proxy`.
  A shared `claude-proxy` container left over from earlier versions is not touched;
  remove it with `docker rm -f claude-proxy`.
- `start.py` refuses a pre-existing `CLAUDE_BRIDGE` that is `--internal` — it would break
  the egress.
- Every running session holds one internal network, and the engine hands each one a
  subnet from its address pools, so a large number of parallel sessions can exhaust them.
  A `claude-internal` network left over from earlier versions is not touched; remove it
  with `docker network rm claude-internal`.
- The sandbox constrains *network* and *filesystem* reach. The dev user cannot escalate
  to root inside the container, but the container is still a container: it is not a
  defense against a kernel exploit.
- Anything Claude writes under a mounted path is written to the real directory on the
  host. Mount only what you want it to be able to change.
- DNS resolution on WSL is a bit wacky, when the host machine is suspended/hibernated. Close all instances
  run `wsl --shutdown` and start again, when the proxy cannot communicate to the outside world.
