import re

from . import check_verb, resolve

NAME = "make"
PRIORITY = 30
MAKEFILES = ("GNUmakefile", "makefile", "Makefile")
TARGET = re.compile(r"^(build|test)\s*:")


def _makefile(repo):
    for name in MAKEFILES:
        path = repo / name
        if path.is_file():
            return path
    return None


def _targets(repo):
    makefile = _makefile(repo)
    if makefile is None:
        return set()
    try:
        lines = makefile.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    return {match.group(1) for match in map(TARGET.match, lines) if match}


def detect(repo):
    return bool(_targets(repo))


def translate(repo, verb, args):
    check_verb(NAME, verb, args)
    if verb not in _targets(repo):
        raise ValueError(f"the Makefile has no {verb!r} target")
    return [resolve("make"), verb]


def describe(repo):
    return {verb: f"make {verb}" for verb in sorted(_targets(repo))}
