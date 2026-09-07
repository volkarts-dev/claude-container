import os

from . import modules


def pick(repo, forced=None):
    name = forced if forced is not None else os.environ.get("CLAUDE_HOST_MODULE")
    if name:
        module = modules.find(name)
        if module is None:
            raise LookupError(f"unknown host exec module {name!r}; known: {', '.join(modules.NAMES)}")
        return module
    matches = [module for module in modules.all_modules() if module.detect(repo)]
    matches.sort(key=lambda module: module.PRIORITY)
    return matches[0] if matches else None
