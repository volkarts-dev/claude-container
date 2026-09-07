import importlib.util
import sys

from . import check_verb

NAME = "python"
PRIORITY = 40
MARKERS = ("pyproject.toml", "pytest.ini")
PACKAGES = {"build": "build", "test": "pytest"}


def _available(package):
    try:
        return importlib.util.find_spec(package) is not None
    except (ImportError, ValueError):
        return False


def _offers(repo, verb):
    if verb == "build" and not (repo / "pyproject.toml").is_file():
        return False
    return _available(PACKAGES[verb])


def detect(repo):
    return any((repo / marker).is_file() for marker in MARKERS)


def translate(repo, verb, args):
    check_verb(NAME, verb, args)
    if verb == "build" and not (repo / "pyproject.toml").is_file():
        raise ValueError("python build needs a pyproject.toml")
    if not _available(PACKAGES[verb]):
        raise ValueError(f"the {PACKAGES[verb]} package is not installed for {sys.executable}")
    return [sys.executable, "-m", PACKAGES[verb]]


def describe(repo):
    return {verb: f"python -m {PACKAGES[verb]}" for verb in ("build", "test") if _offers(repo, verb)}
