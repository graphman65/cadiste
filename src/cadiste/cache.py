"""Traces and scenes kept on disk, so a part is traced again only when something it could depend on changed.

An entry is valid for one fingerprint of:

- every file under the model project's source directories (path, size and modification time: an edit, a checkout,
  a changed data file all show), which is coarser than what a part reads but cannot miss a change;
- cadiste's own source (it decides what a trace contains) and the versions of the CAD libraries;
- the environment traced builds get, and the kind of entry (a trace, a scene).

The latest eight generations per target are retained so captured views can request their exact geometry.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
from collections.abc import Callable, Iterable
from importlib.metadata import version
from pathlib import Path

from .process import locked

_LIBRARIES = ("build123d", "cadquery-ocp-novtk", "ocp-tessellate")
SHAPES_SUFFIX = ".shapes"
"""An entry's shapes (``cadiste.measure``) sit next to its JSON, under the same name with this suffix."""
_GENERATIONS = 8


def _library_versions() -> str:
    found = []
    for name in _LIBRARIES:
        try:
            found.append(f"{name}={version(name)}")
        except Exception:  # not installed under that distribution name
            found.append(f"{name}=?")
    return ";".join(found)


def _files(root: Path, exclude: Path | None = None) -> Iterable[Path]:
    root = root.resolve()
    if exclude is not None and root.is_relative_to(exclude):
        return
    if root.is_file():
        yield root
        return
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = sorted(
            d for d in subdirectories if d != "__pycache__" and not d.startswith(".") and Path(directory, d) != exclude
        )
        for file in sorted(files):
            if not file.endswith((".pyc", ".pyo")):
                yield Path(directory) / file


def fingerprint(roots: Iterable[Path], salt: str = "", *, exclude: Path | None = None) -> str:
    digest = hashlib.sha256(salt.encode())
    excluded = exclude.resolve() if exclude is not None else None
    for root in roots:
        for file in _files(root, excluded):
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
        self.salt = f"{salt};{_library_versions()};storage=2"
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def fingerprint(self) -> str:
        return fingerprint(self.roots, self.salt, exclude=self.directory)

    def _path(self, kind: str, name: str, stamp: str) -> Path:
        key = hashlib.sha256(f"{kind}\0{name}".encode()).hexdigest()[:8]
        return self.directory / f"{kind}-{_slug(name)}-{key}-{stamp[:16]}"

    def latest(self, kind: str, name: str) -> Path | None:
        """The latest retained entry for a target, regardless of its current source fingerprint."""
        prefix = self._path(kind, name, "").name
        entries = sorted(self.directory.glob(f"{prefix}*.json"), key=lambda path: path.stat().st_mtime_ns)
        return entries[-1] if entries else None

    def captured(self, kind: str, name: str, build_id: str) -> Path | None:
        """Resolve an exact target-bound generation; never substitute a newer build."""
        prefix = self._path(kind, name, "").name
        if not re.fullmatch(r"[\w.-]+", build_id) or not build_id.startswith(prefix):
            return None
        path = self.directory / f"{build_id}.json"
        return path if path.is_file() else None

    def get_or_make(
        self,
        kind: str,
        name: str,
        make: Callable[[Path], bytes | None],
        fresh: bool = False,
        *,
        cancel: threading.Event | None = None,
    ) -> tuple[bytes | None, bool, Path | None]:
        """The entry, made (and kept) when missing or ``fresh``; whether it came from the cache; and the file of
        its shapes, if the build wrote one (``make`` gets where to write it). One build at a time per entry: a
        second request for the same part waits for the first instead of building again."""
        with self._guard:
            lock = self._locks.setdefault(f"{kind}\0{name}", threading.Lock())
        with locked(lock, cancel):
            stamp = self.fingerprint()
            prefix = self._path(kind, name, "").name
            entries = sorted(
                self.directory.glob(f"{self._path(kind, name, stamp).name}-*.json"),
                key=lambda entry: entry.stat().st_mtime_ns,
            )
            if not fresh and entries:
                path = entries[-1]
                shapes = path.with_suffix(SHAPES_SUFFIX)
                return path.read_bytes(), True, shapes if shapes.is_file() else None
            # A fresh build must never overwrite an identity still held by a viewer tab.
            path = self.directory / f"{self._path(kind, name, stamp).name}-{secrets.token_hex(8)}.json"
            shapes = path.with_suffix(SHAPES_SUFFIX)
            self.directory.mkdir(parents=True, exist_ok=True)
            staged_shapes = shapes.with_name(f"{shapes.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            body = make(staged_shapes)
            if body is None:
                staged_shapes.unlink(missing_ok=True)
                return None, False, None
            # Publish the new generation before pruning complete old generations.
            staging = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            staging.write_bytes(body)
            if staged_shapes.is_file():
                staged_shapes.replace(shapes)
            staging.replace(path)
            generations = sorted(
                self.directory.glob(f"{prefix}*.json"), key=lambda entry: entry.stat().st_mtime_ns, reverse=True
            )
            for stale in generations[_GENERATIONS:]:
                stale.unlink(missing_ok=True)
                stale.with_suffix(SHAPES_SUFFIX).unlink(missing_ok=True)
            return body, False, shapes if shapes.is_file() else None
