import json

from . import VERBS, check_verb, resolve

NAME = "npm"
PRIORITY = 10


def _scripts(repo):
    manifest = repo / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read {manifest.name}: {error}")
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def _manager(repo):
    if (repo / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (repo / "yarn.lock").is_file():
        return "yarn"
    return "npm"


def detect(repo):
    if not (repo / "package.json").is_file():
        return False
    try:
        scripts = _scripts(repo)
    except ValueError:
        return False
    return any(verb in scripts for verb in VERBS)


def translate(repo, verb, args):
    check_verb(NAME, verb, args)
    if verb not in _scripts(repo):
        raise ValueError(f"package.json has no {verb!r} script")
    return [resolve(_manager(repo)), "run", verb]


def describe(repo):
    manager = _manager(repo)
    scripts = _scripts(repo)
    return {verb: f"{manager} run {verb}" for verb in VERBS if verb in scripts}
