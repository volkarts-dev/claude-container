import re

from . import check_verb, resolve

NAME = "dotnet"
PRIORITY = 20
SOLUTIONS = (".sln", ".slnx")
PROJECTS = (".csproj", ".fsproj", ".vbproj")
CONFIGURATION = re.compile(r"^(Debug|Release)$")


def _project(repo):
    try:
        entries = sorted(path for path in repo.iterdir() if path.is_file())
    except OSError:
        return None
    for suffixes in (SOLUTIONS, PROJECTS):
        found = [path for path in entries if path.suffix in suffixes]
        if len(found) == 1:
            return found[0]
        if found:
            return None
    return None


def _configuration(args):
    if not args:
        return []
    if len(args) == 1 and args[0].startswith("--configuration="):
        value = args[0].split("=", 1)[1]
    elif len(args) == 2 and args[0] in ("-c", "--configuration"):
        value = args[1]
    else:
        raise ValueError("dotnet verbs accept only --configuration Debug|Release")
    if not CONFIGURATION.match(value):
        raise ValueError(f"configuration {value!r} is not Debug or Release")
    return ["--configuration", value]


def detect(repo):
    return _project(repo) is not None


def translate(repo, verb, args):
    check_verb(NAME, verb, args, accepts_args=True)
    project = _project(repo)
    if project is None:
        raise ValueError("expected exactly one solution or project file at the top level")
    return [resolve("dotnet"), verb, project.name, *_configuration(args)]


def describe(repo):
    project = _project(repo)
    if project is None:
        return {}
    return {verb: f"dotnet {verb} {project.name}" for verb in ("build", "test")}
