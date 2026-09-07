import importlib
import shutil

NAMES = ("npm", "dotnet", "make", "python")
VERBS = ("build", "test")


def load(name):
    return importlib.import_module(f"{__name__}.{name}")


def all_modules():
    return [load(name) for name in NAMES]


def find(name):
    return load(name) if name in NAMES else None


def resolve(tool):
    path = shutil.which(tool)
    if not path:
        raise ValueError(f"{tool} is not installed on the host or not on its PATH")
    return path


def check_verb(module, verb, args, accepts_args=False):
    if verb not in VERBS:
        raise ValueError(f"unknown verb {verb!r}; the verbs are build and test")
    if args and not accepts_args:
        raise ValueError(f"{module} {verb} takes no arguments")
