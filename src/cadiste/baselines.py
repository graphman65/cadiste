"""Immutable target-set snapshots, independent of rolling cache generations and warmup."""

from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .server import Build, Builds

MAX_TARGETS = 64
MAX_BASELINES = 128


def _target(value: Any, builds: Builds) -> dict[str, Any]:
    # Share normalization without importing server (server owns Builds and constructs this store).
    from .verification import _target as normalize

    return normalize(value, builds)


class Baselines:
    """Explicit pin/list/get/release API. No background build can mutate a pinned snapshot.

    Pin reuses latest retained captures, building only missing targets. Publication is all-or-nothing;
    names are labels, never identities or overwrite keys. IDs are local to this cache/project store.
    """

    def __init__(self, builds: Builds):
        self.builds = builds
        self.directory = builds.cache.directory / "baselines"
        self._lock = threading.RLock()

    def _path(self, identifier: str) -> Path:
        if not isinstance(identifier, str) or re.fullmatch(r"baseline-[0-9a-f]{32}", identifier) is None:
            raise LookupError("invalid baseline id")
        return self.directory / identifier

    def _manifest(self, identifier: str) -> dict[str, Any]:
        path = self._path(identifier) / "manifest.json"
        if not path.is_file():
            raise LookupError(f"baseline {identifier!r} is unavailable or released")
        try:
            value = json.loads(path.read_bytes())
        except (OSError, ValueError) as error:
            raise LookupError(f"baseline {identifier!r} manifest is unavailable: {error}") from error
        if not isinstance(value, dict) or value.get("id") != identifier or not isinstance(value.get("targets"), list):
            raise LookupError(f"baseline {identifier!r} manifest is invalid")
        return value

    def pin(self, targets: list[dict[str, Any]], name: str | None = None) -> dict[str, Any]:
        if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
            raise ValueError(f"pin requires 1 to {MAX_TARGETS} explicit targets")
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 200):
            raise ValueError("baseline name must contain 1 to 200 characters")
        normalized: list[tuple[dict[str, Any], str | None]] = []
        for value in targets:
            if not isinstance(value, dict) or set(value) - {"kind", "name", "path", "build_id"}:
                raise ValueError("baseline targets accept kind, name/path and optional build_id only")
            target = _target(value, self.builds)
            build_id = value.get("build_id")
            if build_id is not None and (not isinstance(build_id, str) or not build_id):
                raise ValueError("baseline target build_id must be a nonempty captured identity")
            existing = next((item for item in normalized if item[0] == target), None)
            if existing is not None and existing[1] != build_id:
                raise ValueError("a baseline cannot contain two generations of the same target")
            if existing is None:
                normalized.append((target, build_id))
        with self._lock:
            if len(self.list()) >= MAX_BASELINES:
                raise ValueError(f"at most {MAX_BASELINES} baselines may be pinned; release one first")
            identifier = f"baseline-{secrets.token_hex(16)}"
            self.directory.mkdir(parents=True, exist_ok=True)
            staging = self.directory / f".{identifier}.tmp"
            staging.mkdir()
            manifest: dict[str, Any] = {
                "id": identifier,
                "name": name,
                "created_at": datetime.now(UTC).isoformat(),
                "targets": [],
            }
            try:
                for index, (target, build_id) in enumerate(normalized):
                    # Never wait on a target build lock while holding the retention lock.
                    with self.builds.cache.generation_lock:
                        build = (
                            self.builds.captured(target, build_id)
                            if build_id is not None
                            else self.builds.latest(target)
                        )
                        if build is not None:
                            self._copy(staging, index, target, build, manifest)
                            continue
                    build = self.builds.get(target, fresh=False)
                    with self.builds.cache.generation_lock:
                        self._copy(staging, index, target, build, manifest)
                (staging / "manifest.json").write_text(json.dumps(manifest, separators=(",", ":")))
                staging.rename(self._path(identifier))
            except BaseException:
                shutil.rmtree(staging)
                raise
            return manifest

    @staticmethod
    def _copy(directory: Path, index: int, target: dict[str, Any], build: Build, manifest: dict[str, Any]) -> None:
        if build.body is None:
            raise ValueError(f"cannot pin failed target {target!r}: {build.failure.get('error', 'build failed')}")
        data = json.loads(build.body)
        if not isinstance(data, dict) or not isinstance(data.get("build_id"), str) or not data["build_id"]:
            raise ValueError(f"cannot pin target {target!r} without a captured build identity")
        (directory / f"{index}.json").write_bytes(build.body)
        has_shapes = build.shapes is not None
        if build.shapes is not None:
            if not build.shapes.is_file():
                raise LookupError(f"target {target!r} generation expired before pinning; pin again explicitly")
            shutil.copyfile(build.shapes, directory / f"{index}.shapes")
        manifest["targets"].append(
            {
                "target": target,
                "build_id": data["build_id"],
                "source_revision": data.get("source_revision"),
                "source_fingerprint": data.get("source_fingerprint"),
                "shapes_available": has_shapes,
            }
        )

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._manifest(path.name) for path in sorted(self.directory.glob("baseline-*")) if path.is_dir()]

    def get(self, identifier: str, target: dict[str, Any]) -> Build:
        from .server import Build

        target = _target(target, self.builds)
        with self._lock:
            manifest = self._manifest(identifier)
            for index, entry in enumerate(manifest["targets"]):
                if entry.get("target") != target:
                    continue
                path = self._path(identifier)
                body_file, shapes = path / f"{index}.json", path / f"{index}.shapes"
                if not body_file.is_file() or (entry.get("shapes_available") and not shapes.is_file()):
                    raise LookupError(f"baseline {identifier!r} captured artifacts are unavailable")
                body = body_file.read_bytes()
                if json.loads(body).get("build_id") != entry.get("build_id"):
                    raise LookupError(f"baseline {identifier!r} captured build identity does not match its manifest")
                return Build(body, True, shapes if entry.get("shapes_available") else None, {})
        raise LookupError(f"baseline {identifier!r} does not contain target {target!r}")

    def release(self, identifier: str) -> dict[str, Any]:
        with self._lock:
            manifest = self._manifest(identifier)
            shutil.rmtree(self._path(identifier))
            return {"id": identifier, "released": True, "targets": manifest["targets"]}
