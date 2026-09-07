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
                              (claude-internal)                            (claude-egress)
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
  │  [throwaway]          │                     │   [reused]           │
  └───────┬──────────▲────┘                     └──────────────────────┘
          │          │ engine exec -i (optional, --host-exec)
          │ bind     │ one stdio session, frames only
          │ mounts   │
          │   ┌──────┴───────────────┐
          │   │  start.py on host    │
          │   │  hostexec server     │  runs `build` / `test` in the workdir
          │   └──────────────────────┘
   /workspace/<name>      ← the current directory (workdir) plus any extra paths
   ~/.claude              ← host config directory, read-write, persists sessions
   ~/.gitconfig           ← read-only
   /opt/hostexec          ← hostrun, relay and the skill, read-only (--host-exec only)
```

**The dev container** (`claude/Dockerfile`) is Debian 13 slim with Node.js (from the
official nodejs.org tarball, checksum verified), the .NET SDK, PowerShell (from the
official GitHub release tarball, checksum verified), `git`, `git-lfs`, `ripgrep`,
`fd-find`, `jq`, build and test tooling, Python 3 and the usual shell utilities. The
Claude Code CLI is installed globally with npm. It runs as an unprivileged user (`dev`
by default) created with the *host's* uid/gid, so files written into the mounted
workspace keep the right ownership. `tini` is the entrypoint so signals and zombie
processes behave.

There is no way to become root inside it: `sudo` is not installed, `su` and the other
switch-user tools are deleted, every setuid/setgid bit in the image is cleared at build
time, and the container runs with `no-new-privileges`, which makes the kernel refuse any
privilege gain from an exec regardless.

**The proxy container** (`proxy/`) is tinyproxy. Its config is generated at start time
by `entrypoint.sh` from the `UPSTREAM_PROXY` and `NO_UPSTREAM` environment variables, so
switching networks never requires a rebuild. `NO_UPSTREAM` entries become `Upstream none`
rules (both bare and dot-prefixed forms of a domain, CIDR blocks passed through), and
`UPSTREAM_PROXY` becomes the catch-all `Upstream http` line. Access is limited to the
private RFC1918/ULA ranges, and `CONNECT` is limited to port 443.

**The two networks** exist because of DNS. The dev container sits only on an `--internal`
network, which has no route off the host — the proxy is its single exit. The proxy also
joins a normal user-defined bridge network, because the engines' *predefined* bridge
networks serve no DNS and an internal network's resolver refuses to forward, which would 
leave the proxy unable to resolve either its upstream or the sites it is asked to fetch.

**The start script** (`start.py`) wires all of this up on every launch: it
resolves the upstream proxy, creates the networks if missing, reuses a running proxy
container — recreating it when the upstream, the no-proxy list or either network has
changed since it was created (tracked via `claude.*` labels) — waits until tinyproxy
actually answers, then runs the dev container with the mounts, proxy variables and
terminal settings in place.

**The host exec package** (`hostexec/`) is the optional bridge described in
[Running build and test on the host](#running-build-and-test-on-the-host): the frame
protocol, the server that spawns the commands, the host end of the exec link, the
detection of the repo's build system and its modules, and under `hostexec/container/`
the `hostrun` client, the `relay` and the skill plugin that are mounted into the
container. Tests live in `tests/` and run with `python -m unittest` from the repo root.

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

Pass arguments through to `claude` itself after `--`:

```sh
./start.py -- --resume
./start.py ../shared-lib -- --model opus
```

```powershell
./start.ps1 ..\shared-lib '--' --model opus
./start.ps1 ..\shared-lib --% -- --model opus
```

Inside a PowerShell session a bare `--` is consumed by PowerShell itself before the
wrapper sees it, so quote it or put the stop-parsing token `--%` in front of it.
`pwsh -File start.ps1 ... -- ...` from another shell needs neither.

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
- `TERM`, `COLORTERM`, `TERM_PROGRAM`, `TERM_PROGRAM_VERSION`, so colours and key
  handling match your terminal. On Windows `start.py` defaults to
  `xterm-256color`/`truecolor` since Windows consoles set neither.
- `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
  `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` — only when set on the host.

Everything else is left outside. The dev container itself is `--rm`: nothing written
outside the mounts survives the session.

## Running build and test on the host

The container has a toolchain, but the host has *your* toolchain: the right SDK
versions, warm caches, local services, signing keys. With `--host-exec` (or
`CLAUDE_HOST_EXEC=1`) Claude can ask the host to run exactly two things, `build` and
`test`, mapped to whatever the working directory's build system is:

```sh
cd ~/projects/my-app
/path/to/claude-container/start.py --host-exec
```

```
host exec: npm (build: npm run build, test: npm run test)
```

`start.py` looks at the top level of the working directory only and picks the first
matching module: `package.json` with a `build`/`test` script (npm, pnpm or yarn from the
lockfile), then exactly one `.sln`/`.slnx` or one `.csproj`/`.fsproj`/`.vbproj`
(`dotnet build`/`dotnet test`, optionally `--configuration Debug|Release`), then a
`Makefile` with `build:`/`test:` targets, then `pyproject.toml`/`pytest.ini`
(`python -m build` / `python -m pytest`, only if those packages are importable by the
Python running `start.py`). `CLAUDE_HOST_MODULE=name` forces one. With no match the
feature stays off and the container is started as usual.

Inside the container the tools are `hostrun build`, `hostrun test` and `hostrun --list`,
plus a Claude Code skill (loaded through `--plugin-dir` from a read-only mount, so it
exists nowhere on the host's Claude config) that tells Claude when and how to use them.
The command runs on the host with the working directory as cwd and a fixed
non-interactive environment (`CI=1`, colours off); stdout, stderr and the exit status
come back unchanged, stdin is relayed, and killing `hostrun` kills the host process and
its children. One command runs at a time; a second one is refused with `busy`. Every
run is logged to `<config dir>/hostexec.log`.

**What this opens up.** `build` on the host means running whatever `package.json`,
the Makefile or the MSBuild files say, and those files live in the mounted workspace
that Claude edits. So with host exec on, Claude can run arbitrary code on the host with
your user's rights by writing it into the build definition. The fixed verb list stops
arbitrary *commands*, not arbitrary *code*. That is why the feature is opt-in per
session, why the verb-to-command mapping lives in this repository (`hostexec/modules/`)
and never in a file under the workspace, why verbs take no free-form arguments (the one
argument that exists is validated against `Debug|Release`), and why commands always run
in the mounted workdir with a fixed environment. Turn it on for repositories you would
run `npm run build` in yourself.

**How the host is reached.** No port, no extra mount for the channel and no change to
the networks: the host opens one `docker exec -i` (or `podman exec -i`) into the
running container and runs `/opt/hostexec/relay` there, which listens on a Unix socket
inside the container's own filesystem (`/tmp/hostexec/sock`). `hostrun` connects to that
socket; the relay multiplexes the connections over the exec's stdin/stdout as length-
prefixed frames; `start.py` demultiplexes them and spawns the command. The direction is
inverted on purpose: nothing inside the container can open anything toward the host,
and the engine socket is never exposed to it. This works the same on Linux and on a
Windows or macOS podman/docker machine, where a bind-mounted Unix socket or a host
listener would not (the container has no route off the host). If the relay dies the
host restarts it; if `start.py` dies the relay sees EOF and open `hostrun` calls fail
with a clear message while the container keeps running without the feature.

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
| `CLAUDE_NET` | `claude-internal` | Internal network name |
| `CLAUDE_BRIDGE` | `claude-egress` | Outward-facing network; must carry DNS |
| `CLAUDE_USERNS` | auto | `--userns` for the dev container |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Host directory mounted as the Claude config |
| `CLAUDE_HOST_EXEC` | unset | `1` enables host exec, same as `--host-exec` |
| `CLAUDE_HOST_MODULE` | auto | Force the host exec module: `npm`, `dotnet`, `make` or `python` |
| `PROXY_IMAGE` / `PROXY_TAG` | `claude-proxy` / `latest` | Proxy image name and tag |
| `PROXY_CONTAINER` | `claude-proxy` | Proxy container name |
| `PROXY_PORT` | unset | Host port to publish the proxy on |
| `PROXY_BIND` | `127.0.0.1` | Host address for that port |

## Notes and limits

- The proxy container outlives the session on purpose (`--restart unless-stopped`), so
  repeated starts are fast. Remove it with `docker rm -f claude-proxy` if you want a
  clean state; the next start rebuilds it.
- `start.py` refuses a pre-existing `CLAUDE_NET` that is not `--internal`, and a
  `CLAUDE_BRIDGE` that is — either would break the containment or the egress.
- The sandbox constrains *network* and *filesystem* reach. The dev user cannot escalate
  to root inside the container, but the container is still a container: it is not a
  defense against a kernel exploit.
- Anything Claude writes under a mounted path is written to the real directory on the
  host. Mount only what you want it to be able to change.
- With `--host-exec`, `start.py` stays in the foreground for the whole session (it hosts
  the server) instead of exec-ing into the engine, and the dev container gets a
  `--name claude-dev-<random>` so the exec can address it. The dev image needs one
  rebuild after this feature was added, for the `/opt/hostexec` PATH entry.
- DNS resolution on WSL is a bit wacky, when the host machine is suspended/hibernated. Close all instances
  run `wsl --shutdown` and start again, when the proxy cannot communicate to the outside world.
