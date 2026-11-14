# Tasks: Composable build environments

Design, decisions and risks: [plan.md](plan.md).

## Shared checks

Steps below refer to these by name.

- **Unit tests:** `python3 -m unittest discover -s tests -v` passes. Runs anywhere.
- **Setuid check** (host, inside the image under test):
  `./start.py [--with …] -s -c 'find / -xdev -perm /6000 -type f; test ! -e /bin/su && echo ok'`
  prints only `ok`.
- **Home ownership check** (host, inside the image under test):
  `./start.py [--with …] -s -c 'find ~ -xdev ! -user "$(id -un)" -print'`
  prints nothing.
- **Both engines:** host steps pass with `CONTAINER_ENGINE=docker` and with
  `CONTAINER_ENGINE=podman` (rootless).

Every task also keeps the README, the `--help` text of `start.py`/`build.py` and the
`EPILOG` environment lists in step with what it changes.

---

## Phase 1: Mechanism, one feature end to end

## Task 1: Hardening as a reusable script in the base image

**Description:** Move the inline hardening `RUN` (clear setuid/setgid bits, delete
`su`, `newgrp`, `chsh`, `chfn`, `gpasswd`) into `claude/harden.sh`, installed as
`/usr/local/sbin/claude-harden`. The base image runs it where the inline step ran today.
Derived images will run it again as their last root step (Task 3). Behaviour of the base
image does not change.

**Acceptance criteria:**
- [ ] `claude/Dockerfile` has no inline hardening `RUN` left and calls `claude-harden` instead
- [ ] Running the script twice is harmless (idempotent)
- [ ] The base image behaves as before: `./start.py` starts claude and `-s` gives a shell

**Verification:**
- [ ] Build succeeds: `./build.py claude`
- [ ] Setuid check passes on the base image
- [ ] Manual check: `./start.py -s -c 'claude --version'`

**Dependencies:** None

**Files likely touched:**
- `claude/Dockerfile`
- `claude/harden.sh` (new)

**Estimated scope:** XS

---

## Task 2: `features.py`: discover, validate, compose, cache key

**Description:** Add the stdlib-only module `features.py` at the repo root, with the
pure logic only and no engine calls yet:
- discover fragments in a directory (`<name>.dockerfile`) and read their `# description:`
  and `# requires:` headers (requires are parsed but not resolved yet; that comes in
  Task 7)
- validate requested names against `^[a-z0-9][a-z0-9-]*$` and against the discovered
  set
- order the selection canonically (alphabetical for now)
- compose the derived Dockerfile text per plan decision 3
- collect `ARG` names declared in the fragments, so the caller can fill them from the
  host environment
- compute the cache key and tag per plan decision 5

Tests use fixture fragments in a temporary directory.

**Acceptance criteria:**
- [ ] Unknown or malformed names raise an error that lists the known features
- [ ] The composed text matches plan decision 3: root-section env override, fragments in canonical order, `claude-harden`, env restore, `USER`, labels
- [ ] The cache key is stable for equal inputs and changes when any of these change: base image ID, fragment content, selected set or build-arg values. Selection order does not change it. The tag falls back to `env-<hash>` beyond 128 characters

**Verification:**
- [ ] Unit tests pass (new `tests/test_features.py`)
- [ ] `python3 -m py_compile features.py` with the oldest available Python 3 (3.8 target: no `tomllib`, no `match`, no `X | Y` type syntax)

**Dependencies:** Task 1 (the script name the composer emits)

**Files likely touched:**
- `features.py` (new)
- `tests/__init__.py` (new)
- `tests/test_features.py` (new)

**Estimated scope:** S

---

## Task 3: `--with` builds environments on demand; .NET becomes the `dotnet` feature

**Description:** First end-to-end slice:
- Move the .NET block (install step, `DOTNET_*` env, `DOTNET_CHANNEL` ARG and its
  runtime libs) from `claude/Dockerfile` into `claude/features/dotnet.dockerfile`.
- Add `--with LIST` to `start.py`: comma-separated and repeatable, added to
  `VALUE_OPTIONS` so `split_args` does not hand it to claude.
- With a non-empty selection, `start.py` needs the base image ID. It composes through
  `features.py`, fills the fragment ARGs from the host environment and passes
  `USER_UID`/`USER_GID`/`USERNAME` like `build.py` does.
- If the tag does not exist yet, `start.py` builds it with the engine, showing the
  output: `-f <temp file>`, context `claude/`.
- The container then runs on the derived tag instead of the base.
- Add `*.dockerfile text eol=lf` to `.gitattributes`.

**Acceptance criteria:**
- [ ] `./start.py --with dotnet -s -c 'dotnet --info'` builds `claude-dev:dotnet-<hash>` on first use and reports the SDK. A second run starts without building
- [ ] Without `--with`, `dotnet` is absent. `--with nope` exits non-zero, naming the known features, before any network or container is created
- [ ] `./start.py --with dotnet --resume` passes `--resume` to claude. `DOTNET_CHANNEL=9.0 ./start.py --with dotnet` yields a different tag

**Verification:**
- [ ] Unit tests pass (add `split_args` cases for `--with`)
- [ ] Build succeeds: `./build.py claude`, then the acceptance commands above
- [ ] Setuid check and home ownership check pass with `--with dotnet`
- [ ] Manual check: `dotnet new console -o /tmp/x && dotnet run --project /tmp/x` inside a `--with dotnet` shell

**Dependencies:** Task 2

**Files likely touched:**
- `start.py`
- `claude/Dockerfile`
- `claude/features/dotnet.dockerfile` (new)
- `.gitattributes`
- `README.md`

**Estimated scope:** M

---

## Task 4: Per-project `.claude-container` and global `CLAUDE_FEATURES`

**Description:**
- Read `features = a, b` from `.claude-container` in the working directory: `#`
  comments allowed, unknown keys warned about rather than fatal, so later keys can be
  added.
- Read `CLAUDE_FEATURES` from the environment.
- Apply plan decision 6: `--with` beats the project file, which beats
  `CLAUDE_FEATURES`. `--with` replaces rather than merges, and `--with ''` means base
  only.
- Print one startup line like `features dotnet, cpp (from .claude-container)`, or
  `features none (base only)`.

**Acceptance criteria:**
- [ ] Precedence and replace semantics behave as described, including `--with ''`
- [ ] Invalid content (unknown feature, malformed line) fails before anything is created and names the file and line
- [ ] The startup line always shows the active set and its source

**Verification:**
- [ ] Unit tests pass (parser and precedence cases)
- [ ] Manual check: a project with `.claude-container` containing `features = dotnet` gets .NET via plain `./start.py`; `./start.py --with ''` in the same project does not

**Dependencies:** Task 3

**Files likely touched:**
- `features.py`
- `start.py`
- `tests/test_features.py`
- `README.md`

**Estimated scope:** S

---

## Checkpoint A: Mechanism (after Tasks 1–4)
- [ ] Unit tests pass
- [ ] Both engines: base build, `--with dotnet` first and cached start, project file honoured
- [ ] Setuid check and home ownership check pass on base and `dotnet`
- [ ] Human review: composed Dockerfile layout, tag naming, file name and format of `.claude-container`, and the open question on the default set, before the remaining extractions

---

## Phase 2: Extract the remaining toolchains

## Task 5: PowerShell becomes the `pwsh` feature

**Description:** Move the PowerShell block (tarball install with checksum, `PWSH_ROOT`
and `POWERSHELL_*` env, symlink) and the runtime libs it needs into
`claude/features/pwsh.dockerfile`. Remove it from the base.

**Acceptance criteria:**
- [ ] `./start.py --with pwsh -s -c 'pwsh -NoProfile -c $PSVersionTable.PSVersion'` prints the version
- [ ] `pwsh` is absent from the base. `--with dotnet,pwsh` works

**Verification:**
- [ ] Build succeeds and acceptance commands pass
- [ ] Setuid check and home ownership check pass with `--with pwsh`

**Dependencies:** Task 3

**Files likely touched:**
- `claude/Dockerfile`
- `claude/features/pwsh.dockerfile` (new)
- `README.md`

**Estimated scope:** S

---

## Task 6: C/C++ toolchain becomes the `cpp` feature

**Description:** Move build-essential, clang, clang-format, clang-tidy, libclang-rt-dev,
pkg-config, cmake, ninja-build, ccache and the architecture-dependent sanitizer runtime list
into `claude/features/cpp.dockerfile`. Remove them from the base.

**Acceptance criteria:**
- [ ] In a `--with cpp` shell, a hello-world builds and runs with gcc and with clang, both with `-fsanitize=address,undefined`
- [ ] `cmake -G Ninja` configures and builds a trivial project
- [ ] `gcc`, `clang` and `cmake` are absent from the base

**Verification:**
- [ ] Build succeeds and acceptance checks pass
- [ ] Setuid check passes with `--with cpp`

**Dependencies:** Task 3

**Files likely touched:**
- `claude/Dockerfile`
- `claude/features/cpp.dockerfile` (new)
- `README.md`

**Estimated scope:** S

---

## Task 7: Feature dependencies, shown with `imaging` pulling in `fonts`

**Description:** Resolve `# requires:` in `features.py`:
- transitive closure
- cycle detection, which is an error naming the cycle
- canonical order becomes a topological sort, with alphabetical order breaking ties
- a requirement on an unknown feature is an error naming the fragment

The startup line distinguishes requested features from pulled-in ones, e.g.
`features imaging (+fonts)`. First users of it:
- `claude/features/fonts.dockerfile` (the four font packages)
- `claude/features/imaging.dockerfile` (imagemagick, librsvg2-bin; `requires: fonts`)

Remove those packages from the base.

**Acceptance criteria:**
- [ ] `--with imaging` builds an image containing the fonts. `rsvg-convert` renders an SVG with text to PNG
- [ ] Cycles and unknown requirements fail with clear messages
- [ ] Order and cache key are independent of request order and of whether a requirement was also requested explicitly

**Verification:**
- [ ] Unit tests pass (closure, cycle, unknown requirement, ordering, key stability)
- [ ] Build succeeds; manual render check above

**Dependencies:** Task 3

**Files likely touched:**
- `features.py`
- `tests/test_features.py`
- `claude/features/fonts.dockerfile` (new)
- `claude/features/imaging.dockerfile` (new)
- `claude/Dockerfile`

**Estimated scope:** M

---

## Task 8: Virtual display becomes the `x11` feature

**Description:** Move xvfb, xauth, the `/tmp/.X11-unix` directory and `ENV DISPLAY=:99`
into `claude/features/x11.dockerfile` (`requires: fonts`). Change
`claude/entrypoint.sh` to start Xvfb only when the `Xvfb` binary exists, and to leave
`DISPLAY` unset otherwise. Host port forwarding in the entrypoint is unchanged.

**Acceptance criteria:**
- [ ] With `--with x11`, Xvfb is up on `:99` at start (`xdpyinfo` or an X client check), and `-e DISPLAY=:5` still picks another display
- [ ] In the base, the entrypoint starts cleanly with no Xvfb error, and `DISPLAY` is unset
- [ ] `--host-port` forwarding still works in both

**Verification:**
- [ ] Build succeeds and acceptance checks pass
- [ ] Manual check: `./start.py --host-port <port>` against a host server, base and `--with x11`

**Dependencies:** Task 7

**Files likely touched:**
- `claude/Dockerfile`
- `claude/entrypoint.sh`
- `claude/features/x11.dockerfile` (new)
- `README.md`

**Estimated scope:** S

---

## Task 9: Graphics headers become the `graphics` feature

**Description:** Move the X11/Wayland/GL development headers (libx11-dev, libxext-dev,
libxrandr-dev, libxinerama-dev, libxcursor-dev, libxi-dev, libxkbcommon-dev,
libwayland-dev, libgl-dev) and glslc into `claude/features/graphics.dockerfile` with
`requires: cpp x11`.

**Acceptance criteria:**
- [ ] `--with graphics` alone pulls in `cpp`, `x11` and `fonts`. GLFW builds from source with CMake and a GLFW window test runs headless under Xvfb
- [ ] None of these headers remain in the base

**Verification:**
- [ ] Build succeeds; manual GLFW build-and-run check
- [ ] Setuid check passes with `--with graphics`

**Dependencies:** Task 6, Task 8

**Files likely touched:**
- `claude/Dockerfile`
- `claude/features/graphics.dockerfile` (new)
- `README.md`

**Estimated scope:** S

---

## Task 10: Playwright becomes the `playwright` feature

**Description:** Move the Playwright step (global install, `playwright install
--with-deps chromium`, `PLAYWRIGHT_BROWSERS_PATH`, chown to the user) into
`claude/features/playwright.dockerfile`. The root-section `NPM_CONFIG_PREFIX=/usr/local`
from the composer has to be in effect here, so the package lands in `/usr/local`, not
in the user's npm prefix.

**Acceptance criteria:**
- [ ] In a `--with playwright` shell, a headless Chromium script loads a page through the proxy
- [ ] The user can still `npm install -g` into `~/.npm-global` and install another browser revision into `PLAYWRIGHT_BROWSERS_PATH`
- [ ] Playwright and Chromium are absent from the base

**Verification:**
- [ ] Build succeeds and acceptance checks pass
- [ ] Home ownership check passes with `--with playwright`

**Dependencies:** Task 3

**Files likely touched:**
- `claude/Dockerfile`
- `claude/features/playwright.dockerfile` (new)
- `README.md`

**Estimated scope:** S

---

## Checkpoint B: Slim base (after Tasks 5–10)
- [ ] Unit tests pass
- [ ] Both engines: each feature builds alone, and the full set `dotnet,pwsh,graphics,imaging,playwright` builds
- [ ] `dpkg-query -W -f '${Package}\n'` of the full set is a superset of the old monolith's package list (save the old list before Task 1)
- [ ] Setuid check and home ownership check pass on the full set
- [ ] Base image size recorded next to the old monolith's size
- [ ] Human review before tooling and docs

---

## Phase 3: Tooling and docs

## Task 11: `build.py` builds the base, prebuilds feature sets, lists features

**Description:**
- `build.py` gets the target `base`, with `claude` kept as an alias.
- `--features LIST` composes and builds that environment ahead of time, through the
  same `features.py` path `start.py` uses, so tags match.
- `--list` prints each feature with its description and requirements.
- `--update` keeps rebuilding the base from scratch. Combined with `--features`, it
  then rebuilds that environment on the new base.

**Acceptance criteria:**
- [ ] `./build.py --features dotnet,cpp` followed by `./start.py --with cpp,dotnet` starts without building
- [ ] `./build.py --list` shows all features with descriptions and requirements
- [ ] `./build.py claude` and `./build.py` with no target still work as before

**Verification:**
- [ ] Unit tests pass (argument parsing)
- [ ] Manual checks above on both engines

**Dependencies:** Tasks 3–10

**Files likely touched:**
- `build.py`
- `features.py`
- `tests/test_build.py` (new)
- `README.md`

**Estimated scope:** S

---

## Task 12: `build.py --prune` removes stale environment images

**Description:** `--prune` removes images labelled `claude.role=env` whose
`claude.base` label is not the current base image ID, and prints what it removed. It
never removes the base, the proxy, or an environment of the current base. An image used
by a running container is skipped with a note rather than forced.

**Acceptance criteria:**
- [ ] After `./build.py --update`, `./build.py --prune` removes the environments of the old base and nothing else
- [ ] An environment in use by a running session survives, with a note

**Verification:**
- [ ] Unit tests pass (selection logic with mocked engine output)
- [ ] Manual check on both engines

**Dependencies:** Task 11

**Files likely touched:**
- `build.py`
- `features.py`
- `tests/test_build.py`
- `README.md`

**Estimated scope:** S

---

## Task 13: README architecture and migration pass

**Description:**
- Rework the README's architecture section: base + features diagram, the feature
  table, how the cache key works, and why runtime `apt` is not possible.
- Add a migration note: without a selection, sessions now get the base only;
  `CLAUDE_FEATURES` restores a global default.
- Add a short guide to writing a feature: fragment rules, headers, ARGs, no `FROM`, root
  context, and that hardening runs after it.
- Update the configuration table.

**Acceptance criteria:**
- [ ] A reader can add a new feature from the README alone
- [ ] Every variable and option introduced in Tasks 1–12 appears in the configuration table and `--help`
- [ ] The README describes the current state in timeless language

**Verification:**
- [ ] Manual check: follow the "writing a feature" guide to add a throwaway feature, then delete it

**Dependencies:** Task 12

**Files likely touched:**
- `README.md`

**Estimated scope:** S

---

## Checkpoint C: Core goal complete (after Tasks 11–13)
- [ ] All Phase 1–3 acceptance criteria met; unit tests pass; both engines verified
- [ ] Open questions 1 and 2 (default set, `build-essential`) settled
- [ ] Human go/no-go on Phase 4

---

## Phase 4 (optional): Runtime toolchains with mise

## Task 14: mise in the base, with `claude` pinned to the base Node

**Description:**
- Install the mise binary in the base from its GitHub release, verifying the checksum
  the way Node and PowerShell are verified today.
- Put the shims directory on `PATH` and set `MISE_TRUSTED_CONFIG_PATHS=/workspace`.
- First find out how the installed `claude` resolves Node. If it is a script with
  `#!/usr/bin/env node`, put a launcher ahead of it on `PATH` that runs the CLI with the
  base image's Node. If it is a native binary, no pinning is needed. Record which case
  applies.
- Without a volume (Task 15), installed tools are gone when the session ends; that is
  fine here.

**Acceptance criteria:**
- [ ] In a session, `mise use node@20 && node -v` in a project directory gives v20 there; outside it, `node -v` is the base Node
- [ ] With a `mise.toml` pinning an old Node (e.g. 16) in the workspace, `claude` still starts and answers a prompt
- [ ] Python and Go install the same way through the proxy (`mise use python@3.12`, `mise use go@latest`)

**Verification:**
- [ ] Build succeeds: `./build.py base`
- [ ] Manual checks above on both engines
- [ ] Setuid check passes on the base

**Dependencies:** Checkpoint C

**Files likely touched:**
- `claude/Dockerfile`
- `claude/claude-launcher.sh` (new, only if pinning is needed)
- `README.md`

**Estimated scope:** S

---

## Task 15: Persistent mise volume shared across sessions

**Description:** `start.py` mounts the named volume `CLAUDE_MISE_VOLUME` (default
`claude-mise`; empty disables it) at the mise data directory. The base image creates
that directory owned by the dev user, so a fresh volume takes its ownership on docker,
with an equivalent for rootless podman if needed. README: what persists, how to wipe
it (`docker volume rm claude-mise`), and `-e GITHUB_TOKEN` against API rate limits.

**Acceptance criteria:**
- [ ] A runtime installed in one session is available in the next without downloading again
- [ ] The volume's content is owned by the dev user on docker and on rootless podman
- [ ] Two sessions running at the same time can both use and install tools. `CLAUDE_MISE_VOLUME=` starts without a volume

**Verification:**
- [ ] Unit tests pass (run-args include the mount, or omit it when disabled)
- [ ] Manual checks above on both engines

**Dependencies:** Task 14

**Files likely touched:**
- `start.py`
- `claude/Dockerfile`
- `tests/test_start.py` (new)
- `README.md`

**Estimated scope:** S

---

## Checkpoint D: Complete
- [ ] All acceptance criteria met; unit tests pass; both engines verified
- [ ] Human review before merge
