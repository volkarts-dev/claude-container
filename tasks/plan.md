# Implementation Plan: Composable build environments

## Overview

The dev image (`claude/Dockerfile`) carries every toolchain at once: .NET, PowerShell,
C/C++ with sanitizers, X11/Wayland/GL headers, Xvfb, Playwright with Chromium,
ImageMagick, fonts. Every project pays for all of it, and every new toolchain makes it
bigger.

This plan splits it into a **slim base image** plus **features**, one Dockerfile
fragment per toolchain. Each project names the features it needs; `start.py` composes
`base + those fragments` into a derived image, builds it on first use and caches it
under a content hash, so later starts with the same set are instant. An optional last
phase adds **mise** to the base, so language runtimes can be installed and switched at
runtime as the unprivileged user, kept across sessions in a named volume.

Installing system packages while a session runs stays impossible by design: the dev
container has no path to root (no sudo, setuid bits cleared, `no-new-privileges`), and
that does not change. Anything that needs `apt` is a feature, built from the host.

Tasks, acceptance criteria and verification steps: [todo.md](todo.md).

## Starting point

- `claude/Dockerfile` is a single image; `build.py` builds it as `claude-dev:latest`,
  and `start.py` refuses to start when it is missing (the proxy image is built on
  demand).
- Hardening is an inline `RUN` near the end of the Dockerfile: clear every setuid/setgid
  bit, delete `su`, `newgrp`, `chsh`, `chfn`, `gpasswd`.
- `claude/entrypoint.sh` always starts Xvfb, and `DISPLAY=:99` is set in the image.
- There is no test suite today. `tests/` and `hostexec/` only hold stale `__pycache__`
  from the removed host exec feature, whose tests used stdlib `unittest`; this plan
  follows that.
- Scripts are stdlib-only Python and must keep running on Python 3.8 (so no `tomllib`)
  on Linux, macOS and Windows.

### Where verification runs

Unit tests run anywhere: `python3 -m unittest discover -s tests -v`. Image builds and
session checks need docker or podman, which the sandbox does not have, so those steps
run on the host. Every checkpoint covers both docker and rootless podman.

## Architecture decisions

1. **Composition, not an inheritance chain.** A chain (`base → dotnet → dotnet-cpp`)
   needs one image per combination. Fragments compose freely: any set of features
   gives one derived image.

2. **A feature is one file, `claude/features/<name>.dockerfile`:** a Dockerfile
   fragment without `FROM` whose steps run as root. Optional header comments:
   ```
   # description: .NET SDK
   # requires: cpp x11
   ```
   An `ARG NAME[=default]` in a fragment takes the host's `NAME` environment variable
   when it is set (e.g. `DOTNET_CHANNEL`), the same way `build.py` handles `NODE_MAJOR`
   today.

3. **Composed Dockerfile layout.** `start.py` writes it to a temporary file and builds
   with `-f <tmp>` and `claude/` as the context:
   ```
   FROM claude-dev:latest
   ARG USERNAME USER_UID USER_GID
   USER root
   ENV HOME=/root NPM_CONFIG_PREFIX=/usr/local      # root steps must not write into /home/dev
   <fragments in canonical order, one or more RUN layers each>
   RUN /usr/local/sbin/claude-harden
   ENV HOME=/home/${USERNAME} NPM_CONFIG_PREFIX=/home/${USERNAME}/.npm-global
   USER ${USERNAME}
   LABEL claude.role=env claude.features=<names> claude.base=<base image id>
   ```
   The base's `HOME` and `NPM_CONFIG_PREFIX` point at the dev user's home. Without the
   override, root steps like `dotnet --info` or `npm install -g` would leave root-owned
   files in `/home/dev` and break `~/.dotnet/tools` and the user's npm prefix.

4. **Hardening becomes a script in the base** (`/usr/local/sbin/claude-harden`). The
   base runs it, and every derived image runs it again as its last root step, so no
   feature can bring back a setuid binary.

5. **Cache key.** sha256 over the base image ID, the composed Dockerfile text and the
   build-arg values. The tag is `<CLAUDE_IMAGE>:<feature names joined by .>-<12 hex>`
   (e.g. `claude-dev:cpp.dotnet-1a2b3c4d5e6f`), falling back to `env-<12 hex>` when
   that exceeds the 128-character tag limit. Rebuilding the base gives it a new ID, so
   every derived image is rebuilt on its next use. `build.py --update` therefore
   updates all environments.

6. **Selection precedence:** `--with LIST` on the command line, then the
   `.claude-container` file in the working directory, then `CLAUDE_FEATURES`, else none
   (base only). `--with` replaces the list from the lower levels instead of merging, and
   `--with ''` means base only. The chosen set and where it came from are printed at
   startup, next to the mount lines. Project file format (stdlib-parseable, room for
   more keys later):
   ```
   # .claude-container
   features = dotnet, cpp
   ```

7. **Only vetted features.** The workspace is writable from inside the sandbox, so
   Claude can edit `.claude-container`. That file may only *name* fragments that exist
   in this repo (names match `^[a-z0-9][a-z0-9-]*$`). It cannot add packages or
   Dockerfile lines; free-form packages are out of scope (see Open Questions).

8. **Shared logic in `features.py`**, next to `start.py` and `build.py`. Both import it
   (the script directory is on `sys.path`) and the unit tests import it directly.

9. **Feature cut** (contents taken from today's Dockerfile):

   | Feature | Contents | Requires |
   | --- | --- | --- |
   | *base* | Debian slim, ca-certificates, curl, wget, gnupg, git, git-lfs, openssh-client, socat, less, jq, ripgrep, fd-find, zip/unzip/xz/tar, procps, psmisc, htop, vim, nano, man-db, locales + TZ, ncurses-term, tini, python3 (+venv, pip), Node.js + Claude Code, user, entrypoint, hardening | — |
   | `dotnet` | .NET SDK, its runtime libs (libicu, libssl3, libkrb5-3, libgssapi-krb5-2, zlib1g, libstdc++6), `DOTNET_*` env | — |
   | `pwsh` | PowerShell, its runtime libs, `POWERSHELL_*` env | — |
   | `cpp` | build-essential, clang, clang-format, clang-tidy, libclang-rt-dev, pkg-config, cmake, ninja-build, ccache, sanitizer runtimes | — |
   | `fonts` | fonts-dejavu, fonts-noto-core, fonts-inter, fonts-jetbrains-mono | — |
   | `imaging` | imagemagick, librsvg2-bin | `fonts` |
   | `x11` | xvfb, xauth, `/tmp/.X11-unix`, `DISPLAY=:99` | `fonts` |
   | `graphics` | X11/Wayland/GL headers (libx11-dev … libgl-dev), libxkbcommon-dev, glslc | `cpp`, `x11` |
   | `playwright` | Playwright + Chromium with system deps, `PLAYWRIGHT_BROWSERS_PATH` | — |

   Node.js stays in the base because Claude Code is an npm package. Other Node versions
   come from mise (Phase 4).

10. **mise (Phase 4, optional).** Installed in the base with its checksum verified. Its
    data directory is on a named volume (`claude-mise`), shims are on `PATH`, and
    `/workspace` is a trusted config path. `claude` itself always runs on the base
    image's Node, so a project that pins an old Node cannot break the agent.

11. **Not doing:**
    - `apt` while a session runs: it contradicts the no-root design.
    - Sidecar toolchain containers driven through the engine socket: that socket is
      root on the host.
    - Image mounts: they depend on the engine and are fragile across glibc versions.

## Dependency graph

```
T1 hardening script
 └─► T2 features.py (discover, validate, compose, cache key)
      └─► T3 --with + build on demand, dotnet extracted   ← first end-to-end slice
           ├─► T4 .claude-container + CLAUDE_FEATURES
           ├─► T5 pwsh
           ├─► T6 cpp ───────────────────────────┐
           ├─► T7 requires + fonts + imaging     │
           │    └─► T8 x11 + conditional Xvfb ───┴─► T9 graphics
           └─► T10 playwright
 T3..T10 ─► T11 build.py base / prebuild / list ─► T12 prune ─► T13 README pass
 Checkpoint C (go/no-go) ─► T14 mise in base ─► T15 persistent mise volume
```

T5–T10 are independent features, but each one deletes lines from `claude/Dockerfile`.
Build them one after another (or merge carefully) rather than in parallel.

## Task List

### Phase 1: Mechanism, one feature end to end
- [ ] Task 1: Hardening as a reusable script in the base image
- [ ] Task 2: `features.py`: discover, validate, compose, cache key
- [ ] Task 3: `--with` builds environments on demand; .NET becomes the `dotnet` feature
- [ ] Task 4: Per-project `.claude-container` and global `CLAUDE_FEATURES`

### Checkpoint A: Mechanism
- [ ] Unit tests pass; `--with dotnet` works on docker and rootless podman
- [ ] Human review of the composition approach before extracting the rest

### Phase 2: Extract the remaining toolchains
- [ ] Task 5: PowerShell becomes the `pwsh` feature
- [ ] Task 6: C/C++ toolchain becomes the `cpp` feature
- [ ] Task 7: Feature dependencies, shown with `imaging` pulling in `fonts`
- [ ] Task 8: Virtual display becomes the `x11` feature
- [ ] Task 9: Graphics headers become the `graphics` feature
- [ ] Task 10: Playwright becomes the `playwright` feature

### Checkpoint B: Slim base
- [ ] Every feature builds alone and the full set covers the old monolith
- [ ] Base size measured against the old image

### Phase 3: Tooling and docs
- [ ] Task 11: `build.py` builds the base, prebuilds feature sets, lists features
- [ ] Task 12: `build.py --prune` removes stale environment images
- [ ] Task 13: README architecture and migration pass

### Checkpoint C: Core goal complete
- [ ] All Phase 1–3 acceptance criteria met
- [ ] Go/no-go on Phase 4

### Phase 4 (optional): Runtime toolchains with mise
- [ ] Task 14: mise in the base, with `claude` pinned to the base Node
- [ ] Task 15: Persistent mise volume shared across sessions

### Checkpoint D: Complete
- [ ] All acceptance criteria met, ready for review

## Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Claude edits `.claude-container` and changes the next session's environment | Med | Only vetted fragment names are accepted (decision 7), and the selected set is printed at every start |
| Root build steps in derived images write into `/home/dev` through the inherited `HOME`/`NPM_CONFIG_PREFIX` | High | Root-section env override (decision 3); the home ownership check runs at every checkpoint |
| A feature brings back a setuid binary | High | `claude-harden` runs last in every derived image; the setuid check runs at every checkpoint |
| A project pins a Node through mise that cannot run Claude Code | High | `claude` is launched with the base Node (Task 14), with an explicit acceptance test using an old Node |
| Default changes from "everything" to "base only", so existing workflows silently lose tools | Med | Startup line names the active features; README migration note; `CLAUDE_FEATURES` restores a global default |
| Native npm/pip modules need a compiler that is no longer in the base | Med | Documented: add `cpp`. Revisit in Open Questions |
| First start with a new feature set takes minutes | Low | Build output is shown, not swallowed; `build.py --features` prebuilds (Task 11) |
| Derived images pile up after base updates | Low | `build.py --prune` (Task 12) |
| docker (BuildKit) and podman (buildah) differ on `-f` outside the context or `ARG` scoping | Med | Checkpoint A verifies both engines before the remaining extractions |
| CRLF checkouts on Windows break fragment line continuations | Med | `*.dockerfile text eol=lf` in `.gitattributes` (Task 3) |
| Two sessions build the same uncached set at the same time | Low | Both builds produce the same tag; the engine handles it. No locking |
| mise shim behaviour without a pinned version, and GitHub API rate limits for mise downloads | Med | Task 14 verifies the fallback to the base Node; README points at `-e GITHUB_TOKEN` |

## Open questions

1. **Default with nothing selected:** base only (as planned), or every feature, which
   matches today's behaviour?
2. **`build-essential` in the base or only in `cpp`?** It is about 200 MB, but native
   npm/pip modules need it in any stack.
3. **Personal features:** should a host directory of your own fragments
   (`CLAUDE_FEATURES_DIR`) sit next to the repo's? Out of scope for now.
4. **Claude Code install:** move it to the native installer so Node can leave the base
   entirely? Out of scope for now.
5. **Free-form `packages = …` in `.claude-container`:** rejected for now (decision 7).
   Revisit only together with a host-side confirmation prompt.
