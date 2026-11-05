"""Captured project Python and source-based identities; no code is executed to navigate them."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path


def code_ref(file: str, qualified: str) -> str:
    """A definition's identity, independent of its current line number."""
    return f"code:{file}::{qualified}"


def split_ref(reference: str) -> tuple[str, str]:
    """Split a code identity; callers resolve it only against captured source keys."""
    file, separator, qualified = reference.removeprefix("code:").partition("::")
    if not reference.startswith("code:") or not separator or not file or not qualified:
        raise ValueError("expected code:<source file>::<qualified symbol>")
    return file, qualified


def source_revision(sources: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(dict(sources), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def capture_sources(files: Iterable[str], root: Path) -> dict[str, str]:
    """Capture model packages, including parameter modules not entered under the geometry tracer.

    Traced files outside the project remain available, but their surrounding packages are not crawled.
    Imported project scripts are included as well as regular packages. Installed dependencies are excluded.
    """
    root = root.resolve()
    paths = {Path(file).resolve() for file in files if Path(file).is_file()}
    for module in tuple(sys.modules.values()):
        file = getattr(module, "__file__", None)
        if not isinstance(file, str) or not file.endswith(".py"):
            continue
        path = Path(file).resolve()
        if path.is_relative_to(root) and not {"site-packages", "dist-packages", ".venv"}.intersection(path.parts):
            paths.add(path)
    packages: set[Path] = set()
    for path in paths:
        if not path.is_relative_to(root):
            continue
        package = path.parent
        if not (package / "__init__.py").is_file():
            continue
        while (package.parent / "__init__.py").is_file() and package.parent.is_relative_to(root):
            package = package.parent
        packages.add(package)
    for package in packages:
        paths.update(package.rglob("*.py"))
    return {
        str(path.relative_to(root)) if path.is_relative_to(root) else str(path): path.read_text()
        for path in sorted(paths)
        if path.is_file()
    }
