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
  │  ripgrep, fd, jq, …   │                     │                      │    direct
  │                       │                     │                      │    egress
  │  --cap-drop NET_ADMIN │                     │   --cap-drop ALL     │
  │  --cap-drop NET_RAW   │                     │   no-new-privileges  │
  │  no-new-privileges    │                     │                      │
  │  no setuid, no su     │                     │                      │
  │  [throwaway]          │                     │   [reused]           │
  └───────┬───────────────┘                     └──────────────────────┘
          │ bind mounts
          │
   /workspace/<name>      ← the current directory (workdir) plus any extra paths
   ~/.claude              ← host config directory, read-write, persists sessions
   ~/.gitconfig           ← read-only
```

**The dev container** (`claude/Dockerfile`) is Debian 13 slim with Node.js (from the
official nodejs.org tarball, checksum verified), the .NET SDK, `git`, `git-lfs`,
`ripgrep`, `fd-find`, `jq`, build tooling, Python 3 and the usual shell utilities. The
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

**The start scripts** (`start.sh` / `start.ps1`) wire all of this up on every launch:
they resolve the upstream proxy, create the networks if missing, reuse a running proxy
container — recreating it when the upstream, the no-proxy list or either network has
changed since it was created (tracked via `claude.*` labels) — wait until tinyproxy
actually answers, then run the dev container with the mounts, proxy variables and
terminal settings in place.

**The build scripts** (`build.sh` / `build.ps1`) build the two images. The `.sh` and
`.ps1` variants are functionally equivalent; use whichever matches your shell.

## Getting started

Build the images once:

```sh
./build.sh                 # both images
./build.sh claude          # just the dev image
./build.sh proxy -- --no-cache
```

```powershell
./build.ps1
./build.ps1 proxy -DockerArgs --no-cache
```

Then start Claude from whatever project you want to work on:

```sh
cd ~/projects/my-app
/path/to/claude-container/start.sh
```

The current directory is mounted at `/workspace/my-app` and becomes the working
directory. The proxy image is built on demand; the dev image is not, so `build.sh` has to
have run first.

## Usage

Mount extra directories alongside the working directory — each lands under `/workspace`
named after its last path component, with a `-2`, `-3` suffix on collisions:

```sh
./start.sh ../shared-lib ~/data
```

Pass arguments through to `claude` itself after `--`:

```sh
./start.sh -- --resume
./start.sh ../shared-lib -- --model opus
```

```powershell
./start.ps1 ..\shared-lib -ClaudeArgs --resume
```

Use podman instead of docker:

```sh
CONTAINER_ENGINE=podman ./start.sh
```

Under rootless podman the scripts default to `--userns keep-id` so the bind-mounted
workspace stays owned by your uid.

### Behind a corporate proxy

```sh
CLAUDE_PROXY=http://proxy.corp:3128 \
CLAUDE_NO_PROXY=.corp.example,10.0.0.0/8 \
./start.sh
```

`CLAUDE_PROXY` falls back to `HTTPS_PROXY`/`HTTP_PROXY` and `CLAUDE_NO_PROXY` to
`NO_PROXY`, so an already-configured shell usually just works. A proxy on `localhost` is
rewritten to the host gateway automatically. Note that tinyproxy talks to its upstream in
cleartext — an `https://` proxy URL will not work, and the scripts warn about it.

### What gets carried into the container

- `~/.claude` (or `$CLAUDE_CONFIG_DIR`) is mounted read-write and `CLAUDE_CONFIG_DIR`
  points at it, so credentials, settings and session history survive. `.claude.json` is
  kept *inside* that directory — mounting it as a single file would break on Claude
  Code's atomic rewrite — and is seeded from `~/.claude.json` on first run.
- `~/.gitconfig` read-only, if present.
- `TERM`, `COLORTERM`, `TERM_PROGRAM`, `TERM_PROGRAM_VERSION`, so colous and key
  handling match your terminal. `start.ps1` defaults to `xterm-256color`/`truecolor`
  since Windows consoles set neither.
- `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
  `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` — only when set on the host.

Everything else is left outside. The dev container itself is `--rm`: nothing written
outside the mounts survives the session.

## Configuration

Both scripts read the same environment variables; `start.ps1` and `build.ps1` also expose
them as named parameters.

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
| `PROXY_IMAGE` / `PROXY_TAG` | `claude-proxy` / `latest` | Proxy image name and tag |
| `PROXY_CONTAINER` | `claude-proxy` | Proxy container name |
| `PROXY_PORT` | unset | Host port to publish the proxy on |
| `PROXY_BIND` | `127.0.0.1` | Host address for that port |

## Notes and limits

- The proxy container outlives the session on purpose (`--restart unless-stopped`), so
  repeated starts are fast. Remove it with `docker rm -f claude-proxy` if you want a
  clean state; the next start rebuilds it.
- `start.sh` refuses a pre-existing `CLAUDE_NET` that is not `--internal`, and a
  `CLAUDE_BRIDGE` that is — either would break the containment or the egress.
- The sandbox constrains *network* and *filesystem* reach. The dev user cannot escalate
  to root inside the container, but the container is still a container: it is not a
  defense against a kernel exploit.
- Anything Claude writes under a mounted path is written to the real directory on the
  host. Mount only what you want it to be able to change.
