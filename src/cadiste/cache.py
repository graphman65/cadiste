"""Traces and scenes kept on disk, so a part is traced again only when something it could depend on changed.

An entry is valid for one fingerprint of:

- every file under the model project's source directories (path, size and modification time: an edit, a checkout,
  a changed data file all show), which is coarser than what a part reads but cannot miss a change;
- cadiste's own source (it decides what a trace contains) and the versions of the CAD libraries;
- the environment traced builds get, and the kind of entry (a trace, a scene).

Writing an entry removes the entries for the same part made under other fingerprints.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from collections.abc import Callable, Iterable
from importlib.metadata import version
from pathlib import Path

_LIBRARIES = ("build123d", "cadquery-ocp-novtk", "ocp-tessellate")
SHAPES_SUFFIX = ".shapes"
"""An entry's shapes (``cadiste.measure``) sit next to its JSON, under the same name with this suffix."""


def _library_versions() -> str:
    found = []
    for name in _LIBRARIES:
        try:
            found.append(f"{name}={version(name)}")
        except Exception:  # not installed under that distribution name
            found.append(f"{name}=?")
    return ";".join(found)


def _files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = sorted(d for d in subdirectories if d != "__pycache__" and not d.startswith("."))
        for file in sorted(files):
            if not file.endswith((".pyc", ".pyo")):
                yield Path(directory) / file


def fingerprint(roots: Iterable[Path], salt: str = "") -> str:
    digest = hashlib.sha256(salt.encode())
    for root in roots:
        for file in _files(root):
            stat = file.stat()
            digest.update(f"{file}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "model"


class TraceCache:
    """Entries by (kind, name) under ``directory``, valid for the current fingerprint of ``roots``."""

    def __init__(self, directory: Path, roots: Iterable[Path], salt: str):
        self.directory = directory
        # cadiste's own Python (it decides what a trace holds), not its built frontend
        self.roots = [*roots, *sorted(Path(__file__).resolve().parent.glob("*.py"))]
        self.salt = f"{salt};{_library_versions()}"
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _path(self, kind: str, name: str, stamp: str) -> Path:
        key = hashlib.sha256(f"{kind}\0{name}".encode()).hexdigest()[:8]
        return self.directory / f"{kind}-{_slug(name)}-{key}-{stamp[:16]}.json"

    def latest(self, kind: str, name: str) -> Path | None:
        """The file of the entry last made for (kind, name), under whatever fingerprint (the model's source may have
        changed since): what the viewer shows, what a new build is compared with. Its shapes, if kept, are next to it
        (``SHAPES_SUFFIX``). Making an entry removes the others, so ask before."""
        file = self._path(kind, name, "").name
        prefix = file[: file.rindex("-") + 1]
        entries = sorted(self.directory.glob(f"{prefix}*.json"), key=lambda path: path.stat().st_mtime_ns)
        return entries[-1] if entries else None

    def get_or_make(
        self, kind: str, name: str, make: Callable[[Path], bytes | None], fresh: bool = False
    ) -> tuple[bytes | None, bool, Path | None]:
        """The entry, made (and kept) when missing or ``fresh``; whether it came from the cache; and the file of
        its shapes, if the build wrote one (``make`` gets where to write it). One build at a time per entry: a
        second request for the same part waits for the first instead of building again."""
        with self._guard:
            lock = self._locks.setdefault(f"{kind}\0{name}", threading.Lock())
        with lock:
            stamp = fingerprint(self.roots, self.salt)
            path = self._path(kind, name, stamp)
            shapes = path.with_suffix(SHAPES_SUFFIX)
            if not fresh and path.is_file():
                return path.read_bytes(), True, shapes if shapes.is_file() else None
            self.directory.mkdir(parents=True, exist_ok=True)
            staged_shapes = shapes.with_name(f"{shapes.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            body = make(staged_shapes)
            if body is None:
                staged_shapes.unlink(missing_ok=True)
                return None, False, None
            prefix = path.name[: path.name.rindex("-") + 1]
            for stale in [*self.directory.glob(f"{prefix}*.json"), *self.directory.glob(f"{prefix}*{SHAPES_SUFFIX}")]:
                stale.unlink(missing_ok=True)
            staging = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            staging.write_bytes(body)
            staging.replace(path)
            if not staged_shapes.is_file():
                return body, False, None
            staged_shapes.replace(shapes)
            return body, False, shapes
