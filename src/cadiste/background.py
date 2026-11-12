"""Debounced source watching and one prioritized, cancellable project warmup worker.

Snapshots describe pending work, not a replacement scene: retained build identities stay available while a
revision is queued, building, or broken. Catalog and geometry subprocesses never run under the state lock.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from collections.abc import Callable, Generator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .server import Build, Builds, _Catalog

type _Key = tuple[str, str]
type _Record = dict[str, Any]


class BackgroundBuilds:
    """Warm every catalog target once per stable source revision, with live viewers first.

    The small timing parameters are injectable so consumers can exercise scheduling without CAD subprocesses.
    Explicit builds retain their own cache policy; warmup always accepts a current cached generation.
    """

    def __init__(
        self,
        builds: Builds,
        *,
        poll_interval: float = 0.5,
        debounce: float = 0.75,
        heartbeat: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._builds = builds
        self._poll_interval = poll_interval
        self._debounce = debounce
        self._heartbeat = heartbeat
        self._monotonic = monotonic
        self._condition = threading.Condition()
        self._scan_lock = threading.Lock()
        self._completion_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._closed = False
        self._fingerprint = ""
        self._revision = 0
        self._stable_at = 0.0
        self._source_error: str | None = None
        self._error: str | None = None
        self._project: dict[str, Any] | None = None
        self._catalog_attempted = 0
        self._catalog_revision = 0
        self._records: dict[_Key, _Record] = {}
        self._pending: set[_Key] = set()
        self._connected: dict[_Key, int] = {}
        self._visits: dict[_Key, int] = {}
        self._visit = 0
        self._version = 0
        self._manual_versions: dict[_Key, int] = {}
        self._direct: dict[_Key, tuple[str, _Record]] = {}

    def start(self) -> None:
        with self._condition:
            if self._started or self._closed:
                return
            self._started = True
            self._threads = [
                threading.Thread(target=self._watch, name="cadiste-source-watch"),
                threading.Thread(target=self._work, name="cadiste-build-worker"),
            ]
            for thread in self._threads:
                thread.start()

    def close(self) -> None:
        """Cancel subprocesses, wake subscribers, and join before the caller removes its temporary cache."""
        with self._condition:
            self._closed = True
            self._stop.set()
            self._condition.notify_all()
            threads = tuple(self._threads)
        for thread in threads:
            thread.join()

    def _key(self, target: dict[str, Any]) -> _Key:
        kind = target["kind"]
        name = target.get("path" if kind == "file" else "name")
        if kind == "assembly" and not name:
            name = next(iter(self._builds.project.assemblies), "")
        if kind not in {"part", "file", "assembly"} or not isinstance(name, str):
            raise ValueError(f"not something to build: {target!r}")
        return kind, name

    @staticmethod
    def _target(key: _Key) -> dict[str, str]:
        kind, name = key
        return {"kind": kind, "path" if kind == "file" else "name": name}

    def _changed(self) -> None:
        self._version += 1
        self._condition.notify_all()

    def _snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "fingerprint": self._fingerprint,
                "project": self._project,
                "error": self._error,
                "targets": list(self._records.values()),
            }
        )

    def events(self, target: dict[str, Any]) -> Generator[dict[str, Any] | None]:
        """Full snapshots and bounded-wait heartbeats; an empty target observes only the catalog."""
        key = self._key(target) if target else None
        with self._condition:
            if self._closed:
                return
            if key is not None:
                self._connected[key] = self._connected.get(key, 0) + 1
                self._visit += 1
                self._visits[key] = self._visit
                self._condition.notify_all()
        version = -1
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda version=version: self._closed or self._version != version,
                        timeout=self._heartbeat,
                    )
                    if self._closed:
                        return
                    if self._version == version:
                        snapshot = None
                    else:
                        version = self._version
                        snapshot = self._snapshot()
                yield snapshot
        finally:
            if key is not None:
                with self._condition:
                    remaining = self._connected[key] - 1
                    if remaining:
                        self._connected[key] = remaining
                    else:
                        del self._connected[key]
                    self._condition.notify_all()

    def _queued(self, key: _Key) -> _Record:
        previous = self._records.get(key, {})
        result: _Record = {"target": self._target(key), "status": "queued"}
        if "build_id" in previous:
            result["build_id"] = previous["build_id"]
        return result

    def _failed_revision(self, error: str) -> None:
        self._project = None
        self._error = error
        self._pending.clear()
        for record in self._records.values():
            record.update(status="error", error=error)
            record.pop("log", None)
        self._changed()

    def _scan(self) -> str | None:
        # Serialize samples as well as their publication: a slow older scan cannot overwrite a newer one.
        with self._scan_lock:
            if self._stop.is_set():
                return None
            try:
                fingerprint = self._builds.cache.fingerprint()
            except Exception as error:
                message = f"source scan failed: {error}"
                with self._condition:
                    if not self._closed and self._source_error != message:
                        self._revision += 1
                        self._source_error = message
                        self._failed_revision(message)
                return None
            with self._condition:
                if self._closed:
                    return None
                if not self._revision or fingerprint != self._fingerprint or self._source_error is not None:
                    self._fingerprint = fingerprint
                    self._revision += 1
                    self._stable_at = self._monotonic() + self._debounce
                    self._source_error = None
                    self._error = None
                    self._project = None
                    self._catalog_revision = 0
                    self._pending = set(self._records)
                    self._records = {key: self._queued(key) for key in self._records}
                    self._direct.clear()
                    self._changed()
                return fingerprint

    def _watch(self) -> None:
        while not self._stop.is_set():
            self._scan()
            self._stop.wait(self._poll_interval)

    def _catalog(self) -> tuple[dict[str, Any], list[_Key]]:
        catalog: _Catalog = self._builds.project.catalog(refresh=True, cancel=self._stop)
        names = list(catalog.names)
        assemblies = list(self._builds.project.assemblies)
        project = {"parts": names, "folders": copy.deepcopy(catalog.folders), "assemblies": assemblies}
        keys: dict[_Key, None] = dict.fromkeys(("part", name) for name in names)
        for name in names:
            path: list[str] = []
            for step in catalog.folders.get(name, []):
                path.append(str(step["name"]))
                keys[("file", "/".join(path))] = None
        keys.update(dict.fromkeys(("assembly", name) for name in assemblies))
        return project, list(keys)

    def _priority(self, key: _Key, order: dict[_Key, int]) -> tuple[int, int, int]:
        tier = 0 if key in self._connected else 1 if key in self._visits else 2
        return tier, -self._visits.get(key, 0), order[key]

    def _work(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
                remaining = self._stable_at - self._monotonic()
                if not self._revision or self._source_error is not None or remaining > 0:
                    self._condition.wait(
                        timeout=min(self._poll_interval, remaining) if remaining > 0 else self._poll_interval
                    )
                    continue
                revision, fingerprint = self._revision, self._fingerprint
                if self._catalog_attempted != revision:
                    self._catalog_attempted = revision
                    key = None
                    manual_version = 0
                elif self._catalog_revision == revision and self._pending:
                    order = {key: index for index, key in enumerate(self._records)}
                    key = min(self._pending, key=lambda key: self._priority(key, order))
                    self._pending.remove(key)
                    self._records[key]["status"] = "building"
                    manual_version = self._manual_versions.get(key, 0)
                    self._changed()
                else:
                    self._condition.wait(timeout=self._poll_interval)
                    continue
            if key is None:
                self._refresh_catalog(revision, fingerprint)
            else:
                self._build(key, revision, fingerprint, manual_version)

    def _refresh_catalog(self, revision: int, fingerprint: str) -> None:
        try:
            project, keys = self._catalog()
            error = None
        except Exception as failure:
            project, keys = None, []
            error = f"catalog refresh failed: {failure}"
        self._scan()
        with self._condition:
            if self._closed or revision != self._revision or fingerprint != self._fingerprint:
                return
            if error is not None:
                self._failed_revision(error)
                return
            self._project = project
            self._error = None
            self._catalog_revision = revision
            records: dict[_Key, _Record] = {}
            for key in keys:
                direct = self._direct.get(key)
                records[key] = dict(direct[1]) if direct is not None and direct[0] == fingerprint else self._queued(key)
            self._records = records
            self._pending = {key for key, record in records.items() if record["status"] == "queued"}
            self._direct = {key: value for key, value in self._direct.items() if key in records}
            self._visits = {
                key: value for key, value in self._visits.items() if key in records or key in self._connected
            }
            self._changed()

    @staticmethod
    def _identity(build: Build) -> tuple[str, str] | None:
        if build.body is None:
            return None
        data = json.loads(build.body)
        if not isinstance(data, dict):
            raise ValueError("build output is not an object")
        fingerprint, build_id = data.get("source_fingerprint"), data.get("build_id")
        if not isinstance(fingerprint, str) or not isinstance(build_id, str) or not build_id:
            raise ValueError("build output has no source fingerprint or build identity")
        return fingerprint, build_id

    def _build(self, key: _Key, revision: int, fingerprint: str, manual_version: int) -> None:
        try:
            build = self._builds.get(self._target(key), False, refresh=False, cancel=self._stop, notify=False)
            identity = self._identity(build)
            failure = dict(build.failure)
            if identity is not None and identity[0] != fingerprint:
                identity = None
                failure = {"error": "build source fingerprint does not match the scheduled revision"}
        except Exception as error:
            identity, failure = None, {"error": str(error) or type(error).__name__}
        self._scan()
        with self._condition:
            if (
                self._closed
                or revision != self._revision
                or fingerprint != self._fingerprint
                or manual_version != self._manual_versions.get(key, 0)
                or key not in self._records
            ):
                return
            record = self._records[key]
            record.pop("error", None)
            record.pop("log", None)
            if identity is not None:
                record.update(status="ready", build_id=identity[1])
            else:
                record.update(status="error", error=failure.get("error", "the build failed"))
                if failure.get("log"):
                    record["log"] = failure["log"]
            self._changed()

    def completed(self, target: dict[str, Any], build: Build) -> None:
        """Report a current direct build without regressing to an older retained generation."""
        try:
            identity = self._identity(build)
            key = self._key(target)
        except (KeyError, ValueError, TypeError):
            return
        if identity is None:
            return
        # Cache publication and callbacks can be scheduled in different orders. Serialize callbacks and
        # require the latest retained identity, so a delayed older callback cannot replace a newer result.
        with self._completion_lock:
            if self._scan() != identity[0]:
                return
            kind, name = key
            try:
                latest = self._builds.cache.latest("trace" if kind == "part" else kind, name)
            except OSError:
                return
            if latest is None or latest.stem != identity[1]:
                return
            with self._condition:
                if self._closed or self._source_error is not None or identity[0] != self._fingerprint:
                    return
                record: _Record = {"target": self._target(key), "status": "ready", "build_id": identity[1]}
                if self._direct.get(key) == (identity[0], record):
                    return
                self._direct[key] = identity[0], record
                self._manual_versions[key] = self._manual_versions.get(key, 0) + 1
                self._pending.discard(key)
                if key in self._records:
                    self._records[key] = record
                    self._changed()
