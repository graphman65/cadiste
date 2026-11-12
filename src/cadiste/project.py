"""What the server knows about the model project before building anything: its parts, the folder each part is
defined in, and the source directories whose changes invalidate cached traces."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import FunctionType
from typing import Any

from .process import run


def load(target: str) -> Any:
    """The object a ``module:attribute`` target names."""
    module_name, _, attribute = target.partition(":")
    if not attribute:
        raise SystemExit(f"expected module:attribute, got {target!r}")
    value: Any = importlib.import_module(module_name)
    for name in attribute.split("."):
        value = getattr(value, name)
    return value


def part_definitions(target: str) -> dict[str, Any]:
    """Part name -> what defines it: a mapping's items, else each name of an iterable (or of a callable's result)."""
    value: Any = load(target)
    if callable(value) and not isinstance(value, Mapping):
        value = value()
    if isinstance(value, Mapping):
        return {str(name): definition for name, definition in value.items()}
    if not isinstance(value, Iterable):
        raise SystemExit(f"{target} names no parts: {type(value).__name__} is not a mapping or an iterable")
    return {str(name): None for name in value}


def _project_callables(value: Any) -> list[Any]:
    """The classes and functions a part definition holds: itself, or its attributes (a catalog entry's build)."""
    if inspect.isclass(value) or inspect.isfunction(value):
        return [value]
    attributes = vars(value).values() if hasattr(value, "__dict__") else ()
    return [item for item in attributes if inspect.isclass(item) or inspect.isfunction(item)]


def _defining_module(value: Any, package: str) -> str | None:
    """The module of the project (``package``) where a part is defined: its class, its build function, or for a
    lambda (``lambda p: Bracket(p)``) the first project class or function it calls."""
    for item in _project_callables(value):
        if inspect.isclass(item) or (isinstance(item, FunctionType) and item.__name__ != "<lambda>"):
            module = item.__module__
            if module.split(".")[0] == package:
                return module
        elif isinstance(item, FunctionType):
            for name in item.__code__.co_names:
                called = item.__globals__.get(name)
                if (inspect.isclass(called) or inspect.isfunction(called)) and called.__module__.split(".")[
                    0
                ] == package:
                    return called.__module__
    return None


def _package_of(module: str) -> str:
    loaded = sys.modules.get(module)
    package = getattr(loaded, "__package__", None)
    return package if package else module.rpartition(".")[0]


type FolderStep = dict[str, str | bool]
"""One level of a part's folder path: ``{"name": "chassis", "file": False}``; ``file`` for a file's level."""


def part_folders(target: str, definitions: Mapping[str, Any]) -> dict[str, list[FolderStep]]:
    """Part name -> the folder it is listed in: the package it is defined in, relative to the packages all parts
    share (``chassis``, ``gears``), or its module when that is directly in the shared package (``electronics``).
    When a package's parts come from more than one file, each file is a level of its own inside it
    (``chassis`` / ``drive_module``). Parts whose definition says nothing about where they come from are listed at
    the top."""
    project = target.partition(":")[0].split(".")[0]
    modules = {name: _defining_module(definition, project) for name, definition in definitions.items()}
    packages = {name: _package_of(module).split(".") for name, module in modules.items() if module is not None}
    if not packages:
        return {name: [] for name in definitions}
    root: list[str] = []
    for parts in zip(*packages.values(), strict=False):
        if len(set(parts)) != 1:
            break
        root.append(parts[0])
    folders: dict[str, list[str]] = {}
    for name, module in modules.items():
        if module is None:
            folders[name] = []
            continue
        relative = packages[name][len(root) :]
        folders[name] = relative or module.split(".")[len(root) : len(root) + 1]
    files: dict[tuple[str, ...], set[str]] = {}
    for name, module in modules.items():
        if module is not None:
            files.setdefault(tuple(folders[name]), set()).add(module)
    steps: dict[str, list[FolderStep]] = {}
    for name, module in modules.items():
        steps[name] = [{"name": step, "file": False} for step in folders[name]]
        # A part defined in the package's __init__ stays at the package level.
        if module is not None and len(files[tuple(folders[name])]) > 1 and module != _package_of(module):
            steps[name].append({"name": module.rpartition(".")[2], "file": True})
    return steps


def source_roots(targets: Iterable[str]) -> list[Path]:
    """The directories (or single files) of the top-level packages the targets live in."""
    roots: set[Path] = set()
    for target in targets:
        spec = importlib.util.find_spec(target.partition(":")[0].split(".")[0])
        if spec is None:
            continue
        if spec.submodule_search_locations:
            roots.update(Path(location).resolve() for location in spec.submodule_search_locations)
        elif spec.origin:
            roots.add(Path(spec.origin).resolve())
    return sorted(roots)


class _CatalogError(ValueError):
    """Discovery failed; the last complete catalog must not be served as current."""


def _discover(
    target: str, *, cancel: threading.Event | None = None
) -> tuple[tuple[str, ...], dict[str, list[FolderStep]]]:
    """Discover names and folders together in the server's interpreter/environment, never importing the model
    into the server. A private, empty bytecode directory also bypasses timestamp/size-valid stale .pyc files."""
    script = """
import contextlib
import json
import sys
from cadiste.project import part_definitions, part_folders
with contextlib.redirect_stdout(sys.stderr):
    definitions = part_definitions(sys.argv[1])
    folders = part_folders(sys.argv[1], definitions)
print(json.dumps({"parts": list(definitions), "folders": folders}))
"""
    with tempfile.TemporaryDirectory(prefix="cadiste-discover-") as directory:
        environment = {
            **os.environ,
            "PYTHONPYCACHEPREFIX": directory,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        try:
            done = run([sys.executable, "-c", script, target], timeout=900, env=environment, cancel=cancel)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise _CatalogError(f"part discovery failed: {error}") from error
    if done.returncode != 0:
        raise _CatalogError(f"part discovery failed:\n{done.stderr.decode(errors='replace').strip()}")
    try:
        data = json.loads(done.stdout)
        return tuple(data["parts"]), data["folders"]
    except (ValueError, KeyError, TypeError) as error:
        raise _CatalogError("part discovery failed: invalid discovery output") from error
