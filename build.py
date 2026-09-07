#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

DESCRIPTION = """\
Builds the container images. TARGET is claude or proxy; without one both are
built.

With --update the latest base images are pulled and the images are rebuilt from
scratch.

Anything after -- is passed on to the engine's build command."""

EPILOG = """\
environment:
  CONTAINER_ENGINE container frontend, docker or podman (default docker)
  CLAUDE_IMAGE     dev image name (default claude-dev)
  CLAUDE_TAG       dev image tag (default latest)
  CONTAINER_USER   user inside the dev image (default dev)
  NODE_MAJOR       Node.js major version (default 22)
  DOTNET_CHANNEL   .NET channel (default 10.0)
  PROXY_IMAGE      proxy image name (default claude-proxy)
  PROXY_TAG        proxy image tag (default latest)"""


def env(name, default):
    return os.environ.get(name) or default


def die(message):
    print(f"build.py: {message}", file=sys.stderr)
    sys.exit(1)


def host_ids():
    if hasattr(os, "getuid"):
        return str(os.getuid()), str(os.getgid())
    return "1000", "1000"


def run_build(engine, args):
    result = subprocess.run([engine, "build", *args])
    if result.returncode != 0:
        sys.exit(result.returncode)


def build_claude(engine, build_args):
    uid, gid = host_ids()
    image = f"{env('CLAUDE_IMAGE', 'claude-dev')}:{env('CLAUDE_TAG', 'latest')}"
    run_build(engine, [
        "--build-arg", f"USER_UID={uid}",
        "--build-arg", f"USER_GID={gid}",
        "--build-arg", f"USERNAME={env('CONTAINER_USER', 'dev')}",
        "--build-arg", f"NODE_MAJOR={env('NODE_MAJOR', '22')}",
        "--build-arg", f"DOTNET_CHANNEL={env('DOTNET_CHANNEL', '10.0')}",
        "-t", image,
        *build_args,
        str(SCRIPT_DIR / "claude"),
    ])
    print(f"built {image}", file=sys.stderr)


def build_proxy(engine, build_args):
    image = f"{env('PROXY_IMAGE', 'claude-proxy')}:{env('PROXY_TAG', 'latest')}"
    run_build(engine, ["-t", image, *build_args, str(SCRIPT_DIR / "proxy")])
    print(f"built {image}", file=sys.stderr)


BUILDERS = {"claude": build_claude, "proxy": build_proxy}


def parse_args(argv):
    build_args = []
    if "--" in argv:
        split = argv.index("--")
        argv, build_args = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(
        prog="build.py",
        usage="%(prog)s [--update] [TARGET...] [-- DOCKER_BUILD_ARG...]",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--update", action="store_true")
    parser.add_argument("targets", nargs="*", metavar="TARGET")
    options = parser.parse_args(argv)
    for target in options.targets:
        if target not in BUILDERS:
            parser.error(f"unknown target: {target}")
    if options.update:
        build_args = ["--pull", "--no-cache", *build_args]
    return options.targets or list(BUILDERS), build_args


def main():
    engine = env("CONTAINER_ENGINE", "docker")
    if engine not in ("docker", "podman"):
        die(f"unknown container engine: {engine}")
    targets, build_args = parse_args(sys.argv[1:])
    for target in targets:
        BUILDERS[target](engine, build_args)


if __name__ == "__main__":
    main()
