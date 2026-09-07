---
name: host-build-test
description: Build the project or run its tests on the host machine with `hostrun build` and `hostrun test`. Use whenever the user asks to build, compile, or run tests, or when you want to verify a change, and prefer it over running the toolchain inside the container.
---

# Building and testing on the host

This container is a sandbox. When the session was started with host exec, the host
machine offers two fixed actions, `build` and `test`, mapped to the repository's real
build system. The host has the real toolchain, caches, SDKs and services, so its result
is the one that counts.

## Check first

Host exec is enabled only when the environment variable `CLAUDE_HOST_EXEC` is set.
Then `hostrun --list` prints the verbs the host offers and the command each runs, e.g.

```
build   npm run build
test    dotnet test app.sln
```

If `CLAUDE_HOST_EXEC` is unset or `hostrun --list` reports nothing, the feature is off
for this session: say so and fall back to the toolchain inside the container.

## Use

```
hostrun build
hostrun test
```

- The command runs on the host in the mounted working directory, with a fixed
  non-interactive environment (`CI=1`, colours off). stdout, stderr and the exit status
  are those of the host tool. A line on stderr echoes the exact command that ran.
- Paths in the output are host paths. The working directory on the host corresponds to
  the current `/workspace/<name>` directory; map the rest of a reported path onto it.
- One command runs at a time. A second call while one is running is refused with
  `busy`; wait for the first to finish.
- To stop a running command, terminate `hostrun` (Ctrl-C, or kill the process); the
  host kills the tool and its children.
- Only these verbs exist and only the arguments `hostrun --list` documents are accepted.
  The `dotnet` module accepts `--configuration Debug|Release`; the others take none.
  There is no way to run other commands on the host, so use the container for anything
  else.
- Right after the session starts, `hostrun` may report the relay as not answering. It
  retries for a few seconds on its own; if it still fails, retry once.
- A missing script or target (`package.json has no 'build' script`) is reported before
  anything runs. Fix the repository definition or use the container.
