from __future__ import annotations

import copy
import json
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from cadiste.background import BackgroundBuilds
from cadiste.server import Build, Builds, _Catalog


def _part(name: str) -> dict[str, str]:
    return {"kind": "part", "name": name}


def _built(fingerprint: str, build_id: str) -> Build:
    body = json.dumps({"source_fingerprint": fingerprint, "build_id": build_id}).encode()
    return Build(body, False, None, {})


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Cache:
    def __init__(self) -> None:
        self.revision = "r1"
        self.error: Exception | None = None
        self.latest_paths: dict[tuple[str, str], Path] = {}
        self.latest_error: OSError | None = None

    def fingerprint(self) -> str:
        if self.error is not None:
            raise self.error
        return self.revision

    def latest(self, kind: str, name: str) -> Path | None:
        if self.latest_error is not None:
            raise self.latest_error
        return self.latest_paths.get((kind, name))


class _Project:
    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names
        self.folders: dict[str, list[dict[str, str | bool]]] = {}
        self.assemblies: dict[str, str] = {}
        self.error: Exception | None = None
        self.calls = 0
        self.block = False
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self.cancelled = threading.Event()

    def catalog(self, *, refresh: bool, cancel: threading.Event) -> _Catalog:
        assert refresh
        self.calls += 1
        catalog = _Catalog(self.names, self.names, copy.deepcopy(self.folders))
        self.entered.set()
        if self.block:
            assert cancel.wait(2), "catalog cancellation was not delivered"
            self.cancelled.set()
            raise InterruptedError("catalog cancelled")
        if self.error is not None:
            raise self.error
        while self.gate is not None and not self.gate.wait(0.005):
            if cancel.is_set():
                self.cancelled.set()
                raise InterruptedError("catalog cancelled")
        return catalog


@dataclass
class _Request:
    target: dict[str, Any]
    revision: str
    release: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    result: Build | Exception | None = None

    def finish(self, result: Build | Exception | None = None) -> None:
        self.result = result
        self.release.set()


class _Builds:
    def __init__(self, names: tuple[str, ...]) -> None:
        self.project = _Project(names)
        self.cache = _Cache()
        self.always_fresh = True
        self.requests: list[_Request] = []
        self.condition = threading.Condition()

    def get(
        self,
        target: dict[str, Any],
        fresh: bool,
        *,
        refresh: bool,
        cancel: threading.Event,
        notify: bool,
    ) -> Build:
        # No-cache on explicit requests must not turn warmup into repeated forced builds or discovery.
        assert not fresh and not refresh and not notify
        request = _Request(target, self.cache.revision)
        with self.condition:
            self.requests.append(request)
            self.condition.notify_all()
        while not request.release.wait(0.005):
            if cancel.is_set():
                request.cancelled.set()
                raise InterruptedError("build cancelled")
        if isinstance(request.result, Exception):
            raise request.result
        return request.result or _built(request.revision, f"build-{len(self.requests)}")

    def request(self, index: int) -> _Request:
        with self.condition:
            assert self.condition.wait_for(lambda: len(self.requests) > index, timeout=2), "no queued build"
            return self.requests[index]


def _until(
    stream: Generator[dict[str, Any] | None],
    predicate,
) -> dict[str, Any]:
    deadline = time.monotonic() + 2
    latest = None
    while time.monotonic() < deadline:
        snapshot = next(stream)
        if snapshot is not None:
            latest = snapshot
            if predicate(snapshot):
                return snapshot
    pytest.fail(f"snapshot condition was not met; last snapshot: {latest!r}")


def _record(snapshot: dict[str, Any], name: str) -> dict[str, Any]:
    return next(record for record in snapshot["targets"] if record["target"] == _part(name))


def _ready(snapshot: dict[str, Any], name: str) -> bool:
    return any(record["target"] == _part(name) and record["status"] == "ready" for record in snapshot["targets"])


class _Harness:
    def __init__(self, names: tuple[str, ...]) -> None:
        self.builds = _Builds(names)
        self.clock = _Clock()
        self.background = BackgroundBuilds(
            cast(Builds, self.builds),
            poll_interval=0.005,
            debounce=0.75,
            heartbeat=0.01,
            monotonic=self.clock,
        )
        self.streams: list[Generator[dict[str, Any] | None]] = []
        self.background.start()

    def watch(self, target: dict[str, str]) -> Generator[dict[str, Any] | None]:
        stream = self.background.events(target)
        self.streams.append(stream)
        _until(stream, lambda snapshot: snapshot["fingerprint"] == self.builds.cache.revision)
        return stream

    def edit(self, stream: Generator[dict[str, Any] | None], revision: str) -> dict[str, Any]:
        self.builds.cache.revision = revision
        return _until(stream, lambda snapshot: snapshot["fingerprint"] == revision)

    def complete(self, target: dict[str, str], build: Build) -> None:
        if build.body is not None:
            data = json.loads(build.body)
            if "build_id" in data:
                kind = "trace" if target["kind"] == "part" else target["kind"]
                name = target["path" if kind == "file" else "name"]
                self.builds.cache.latest_paths[kind, name] = Path(f"{data['build_id']}.json")
        self.background.completed(target, build)

    def stable(self) -> None:
        self.clock.advance(0.75)

    def close(self) -> None:
        self.background.close()
        for stream in self.streams:
            stream.close()


@pytest.fixture
def make_background():
    harnesses: list[_Harness] = []

    def make(names: tuple[str, ...] = ("A",)) -> _Harness:
        harness = _Harness(names)
        harnesses.append(harness)
        return harness

    yield make
    for harness in harnesses:
        harness.close()


def test_initial_debounce_prioritizes_viewer_then_warms_parts_folder_ancestors_and_assemblies(make_background):
    harness = make_background(("A", "B"))
    builds = harness.builds
    builds.project.folders = {"B": [{"name": "package", "file": False}, {"name": "pieces", "file": True}]}
    builds.project.assemblies = {"Whole": "model:assembly"}
    stream = harness.watch({"kind": "assembly", "name": "Whole"})
    harness.clock.advance(0.74)
    assert next(stream) is None
    assert builds.project.calls == 0 and builds.requests == []
    harness.clock.advance(0.02)

    expected = [
        {"kind": "assembly", "name": "Whole"},
        _part("A"),
        _part("B"),
        {"kind": "file", "path": "package"},
        {"kind": "file", "path": "package/pieces"},
    ]
    for index, target in enumerate(expected):
        request = builds.request(index)
        assert request.target == target
        request.finish()
    snapshot = _until(
        stream,
        lambda snapshot: (
            len(snapshot["targets"]) == 5 and all(record["status"] == "ready" for record in snapshot["targets"])
        ),
    )
    assert snapshot["project"] == {
        "parts": ["A", "B"],
        "folders": builds.project.folders,
        "assemblies": ["Whole"],
    }
    assert snapshot["error"] is None
    assert next(stream) is None
    assert builds.project.calls == 1 and len(builds.requests) == 5


def test_connected_targets_precede_recent_visits_and_disconnection_counts_every_subscriber(make_background):
    harness = make_background(("A", "B", "C", "D", "E"))
    for name in ("D", "C", "D"):
        harness.watch(_part(name)).close()
    harness.watch(_part("E"))
    harness.watch(_part("E")).close()
    harness.watch(_part("D")).close()  # More recent than E, but E still has a connected subscriber.
    stream = harness.watch(_part("B"))
    harness.stable()
    for index, name in enumerate(("B", "E", "D", "C", "A")):
        request = harness.builds.request(index)
        assert request.target == _part(name)
        request.finish()
    _until(stream, lambda snapshot: all(record["status"] == "ready" for record in snapshot["targets"]))


def test_rapid_edits_coalesce_into_one_catalog_refresh_and_one_stable_revision(make_background):
    harness = make_background()
    stream = harness.watch(_part("A"))
    harness.clock.advance(0.4)
    harness.edit(stream, "r2")
    harness.clock.advance(0.4)
    harness.edit(stream, "r3")
    harness.clock.advance(0.74)
    assert next(stream) is None
    assert harness.builds.project.calls == 0 and harness.builds.requests == []
    harness.clock.advance(0.02)
    request = harness.builds.request(0)
    assert request.revision == "r3"
    request.finish()
    _until(stream, lambda snapshot: _ready(snapshot, "A"))
    assert next(stream) is None
    assert harness.builds.project.calls == 1 and len(harness.builds.requests) == 1


def test_edit_during_build_rejects_old_scene_and_rebuilds_current_target_first(make_background):
    harness = make_background(("A", "B"))
    stream = harness.watch(_part("B"))
    harness.stable()
    old = harness.builds.request(0)
    assert old.target == _part("B")
    harness.edit(stream, "r2")
    harness.stable()
    old.finish(_built("r1", "obsolete"))
    current = harness.builds.request(1)
    assert current.target == _part("B") and current.revision == "r2"
    snapshot = _until(stream, lambda snapshot: _record(snapshot, "B")["status"] == "building")
    assert snapshot["fingerprint"] == "r2"
    assert all(record.get("build_id") != "obsolete" for record in snapshot["targets"])
    current.finish(_built("r2", "current"))
    harness.builds.request(2).finish()
    snapshot = _until(stream, lambda snapshot: _ready(snapshot, "A") and _ready(snapshot, "B"))
    assert _record(snapshot, "B")["build_id"] == "current"
    assert harness.builds.project.calls == 2


def test_direct_completion_supersedes_older_worker_and_removes_pending_target(make_background):
    harness = make_background(("A", "B", "C"))
    stream = harness.watch(_part("A"))
    harness.stable()
    old = harness.builds.request(0)
    harness.complete(_part("A"), _built("r1", "manual-new"))
    harness.complete(_part("B"), _built("r1", "manual-pending"))
    _until(stream, lambda snapshot: _ready(snapshot, "A") and _ready(snapshot, "B"))
    old.finish(_built("r1", "worker-old"))
    following = harness.builds.request(1)
    assert following.target == _part("C")
    following.finish()
    snapshot = _until(stream, lambda snapshot: _ready(snapshot, "C"))
    assert _record(snapshot, "A")["build_id"] == "manual-new"
    assert _record(snapshot, "B")["build_id"] == "manual-pending"
    assert next(stream) is None
    assert len(harness.builds.requests) == 2


def test_current_direct_completion_before_catalog_is_retained_without_duplicate_warmup(make_background):
    harness = make_background(("A", "B"))
    stream = harness.watch(_part("A"))
    built = _built("r1", "direct-first")
    harness.complete(_part("A"), built)
    harness.complete(_part("A"), built)
    harness.stable()
    request = harness.builds.request(0)
    assert request.target == _part("B")
    request.finish()
    snapshot = _until(stream, lambda snapshot: _ready(snapshot, "B"))
    assert _record(snapshot, "A")["build_id"] == "direct-first"
    assert next(stream) is None and len(harness.builds.requests) == 1


def test_direct_completion_requires_current_source_evidence(make_background):
    harness = make_background()
    stream = harness.watch(_part("A"))
    harness.builds.cache.revision = "r2"
    harness.complete(_part("A"), _built("r1", "stale-direct"))
    snapshot = _until(stream, lambda snapshot: snapshot["fingerprint"] == "r2")
    assert not _ready(snapshot, "A")
    harness.complete(_part("A"), Build(b"{}", False, None, {}))
    harness.complete(_part("A"), Build(None, False, None, {"error": "unknown revision"}))
    harness.stable()
    request = harness.builds.request(0)
    assert request.revision == "r2"
    request.finish()
    snapshot = _until(stream, lambda snapshot: _ready(snapshot, "A"))
    assert _record(snapshot, "A")["build_id"] != "stale-direct"


@pytest.mark.parametrize(
    "failure",
    [
        Build(None, False, None, {"error": "bad geometry", "log": "traceback details"}),
        RuntimeError("bad geometry"),
    ],
)
def test_build_failure_preserves_last_scene_does_not_retry_and_recovers_after_edit(make_background, failure):
    harness = make_background()
    stream = harness.watch(_part("A"))
    harness.stable()
    harness.builds.request(0).finish(_built("r1", "last-good"))
    _until(stream, lambda snapshot: _ready(snapshot, "A"))
    queued = harness.edit(stream, "r2")
    assert _record(queued, "A") == {"target": _part("A"), "status": "queued", "build_id": "last-good"}
    harness.stable()
    harness.builds.request(1).finish(failure)
    snapshot = _until(stream, lambda snapshot: _record(snapshot, "A")["status"] == "error")
    record = _record(snapshot, "A")
    assert record["error"] == "bad geometry" and record["build_id"] == "last-good"
    if isinstance(failure, Build):
        assert record["log"] == "traceback details"
    assert next(stream) is None
    assert len(harness.builds.requests) == 2
    harness.edit(stream, "r3")
    harness.stable()
    harness.builds.request(2).finish(_built("r3", "recovered"))
    snapshot = _until(stream, lambda snapshot: _ready(snapshot, "A"))
    assert _record(snapshot, "A") == {"target": _part("A"), "status": "ready", "build_id": "recovered"}


def test_catalog_failure_is_explicit_without_retry_and_next_edit_replaces_deleted_targets(make_background):
    harness = make_background()
    stream = harness.watch(_part("A"))
    harness.stable()
    harness.builds.request(0).finish(_built("r1", "last-good"))
    _until(stream, lambda snapshot: _ready(snapshot, "A"))
    harness.builds.project.error = ValueError("bad catalog")
    harness.edit(stream, "r2")
    harness.stable()
    failed = _until(stream, lambda snapshot: snapshot["error"] is not None)
    assert failed["project"] is None and "bad catalog" in failed["error"]
    assert _record(failed, "A")["build_id"] == "last-good"
    assert _record(failed, "A")["status"] == "error"
    assert next(stream) is None
    assert harness.builds.project.calls == 2 and len(harness.builds.requests) == 1

    harness.builds.project.error = None
    harness.builds.project.names = ("B",)
    harness.edit(stream, "r3")
    harness.stable()
    request = harness.builds.request(1)
    assert request.target == _part("B")
    request.finish()
    snapshot = _until(stream, lambda snapshot: _ready(snapshot, "B"))
    assert snapshot["project"]["parts"] == ["B"] and snapshot["error"] is None
    assert [record["target"] for record in snapshot["targets"]] == [_part("B")]
    assert len(harness.builds.requests) == 2


def test_target_deleted_during_inflight_build_is_not_requeued(make_background):
    harness = make_background(("A", "B"))
    stream = harness.watch(_part("A"))
    harness.stable()
    old = harness.builds.request(0)
    harness.builds.project.names = ("B", "C")
    harness.edit(stream, "r2")
    harness.stable()
    old.finish(_built("r1", "deleted"))
    for index, name in enumerate(("B", "C"), start=1):
        request = harness.builds.request(index)
        assert request.target == _part(name)
        request.finish()
    snapshot = _until(stream, lambda snapshot: _ready(snapshot, "B") and _ready(snapshot, "C"))
    assert snapshot["project"]["parts"] == ["B", "C"]
    assert all(record["target"] != _part("A") for record in snapshot["targets"])


def test_edit_during_catalog_discards_old_catalog_without_blocking_source_notifications(make_background):
    harness = make_background()
    stream = harness.watch(_part("A"))
    gate = threading.Event()
    harness.builds.project.gate = gate
    harness.stable()
    assert harness.builds.project.entered.wait(2)
    harness.builds.project.names = ("B",)
    snapshot = harness.edit(stream, "r2")
    assert snapshot["project"] is None
    harness.stable()
    gate.set()
    request = harness.builds.request(0)
    assert request.target == _part("B") and request.revision == "r2"
    request.finish()
    snapshot = _until(stream, lambda snapshot: _ready(snapshot, "B"))
    assert snapshot["project"]["parts"] == ["B"]
    assert [record["target"] for record in snapshot["targets"]] == [_part("B")]
    assert harness.builds.project.calls == 2


def test_source_scan_failure_is_explicit_and_recovers_without_losing_retained_scene(make_background):
    harness = make_background()
    stream = harness.watch(_part("A"))
    harness.stable()
    harness.builds.request(0).finish(_built("r1", "last-good"))
    _until(stream, lambda snapshot: _ready(snapshot, "A"))
    harness.builds.cache.error = OSError("source unavailable")
    snapshot = _until(stream, lambda snapshot: snapshot["error"] is not None)
    assert snapshot["error"] == "source scan failed: source unavailable"
    assert _record(snapshot, "A")["build_id"] == "last-good"
    assert next(stream) is None and len(harness.builds.requests) == 1
    harness.builds.cache.revision = "r2"
    harness.builds.cache.error = None
    _until(stream, lambda snapshot: snapshot["fingerprint"] == "r2" and snapshot["error"] is None)
    harness.stable()
    harness.builds.request(1).finish()
    _until(stream, lambda snapshot: _ready(snapshot, "A"))


def test_reconnect_gets_independent_full_snapshot_and_heartbeats_without_rebuilding(make_background):
    harness = make_background()
    stream = harness.watch(_part("A"))
    harness.stable()
    harness.builds.request(0).finish(_built("r1", "retained"))
    snapshot = _until(stream, lambda snapshot: _ready(snapshot, "A"))
    snapshot["targets"].clear()
    snapshot["project"]["parts"].clear()
    stream.close()
    reconnected = harness.background.events(_part("A"))
    harness.streams.append(reconnected)
    snapshot = next(reconnected)
    assert snapshot is not None and snapshot["project"]["parts"] == ["A"]
    assert _record(snapshot, "A")["build_id"] == "retained"
    assert next(reconnected) is None
    assert harness.builds.project.calls == 1 and len(harness.builds.requests) == 1


@pytest.mark.parametrize("phase", ["catalog", "build"])
def test_close_cancels_inflight_work_joins_and_stops_event_streams(make_background, phase):
    harness = make_background()
    stream = harness.watch(_part("A"))
    harness.builds.project.block = phase == "catalog"
    harness.stable()
    if phase == "catalog":
        assert harness.builds.project.entered.wait(2)
        cancelled = harness.builds.project.cancelled
    else:
        cancelled = harness.builds.request(0).cancelled
    harness.background.close()
    assert cancelled.is_set()
    with pytest.raises(StopIteration):
        next(stream)
    harness.background.close()
    harness.background.start()
    assert not any(thread.name in {"cadiste-source-watch", "cadiste-build-worker"} for thread in threading.enumerate())


def test_close_before_start_does_not_start_work_or_register_subscriber():
    builds = _Builds(("A",))
    background = BackgroundBuilds(cast(Builds, builds))
    background.close()
    background.start()
    assert list(background.events(_part("A"))) == []
    assert builds.project.calls == 0 and builds.requests == []


def test_catalog_only_stream_observes_parts_added_to_initially_empty_project(make_background):
    harness = make_background(())
    stream = harness.watch({})
    harness.stable()
    snapshot = _until(stream, lambda snapshot: snapshot["project"] is not None)
    assert snapshot["project"] == {"parts": [], "folders": {}, "assemblies": []}
    assert snapshot["targets"] == []
    assert next(stream) is None and harness.builds.requests == []
    harness.builds.project.names = ("A",)
    harness.edit(stream, "r2")
    harness.stable()
    request = harness.builds.request(0)
    assert request.target == _part("A")
    request.finish()
    snapshot = _until(stream, lambda snapshot: _ready(snapshot, "A"))
    assert snapshot["project"]["parts"] == ["A"]
    stream.close()


def test_delayed_direct_callback_cannot_replace_newer_retained_manual_generation(make_background):
    harness = make_background(("A", "B"))
    stream = harness.watch(_part("A"))
    harness.stable()
    worker = harness.builds.request(0)
    older = _built("r1", "manual-old")
    newer = _built("r1", "manual-new")
    harness.complete(_part("A"), older)
    harness.complete(_part("A"), newer)
    harness.background.completed(_part("A"), older)
    snapshot = _until(stream, lambda snapshot: _ready(snapshot, "A"))
    assert _record(snapshot, "A")["build_id"] == "manual-new"
    harness.builds.cache.latest_error = FileNotFoundError("cache entry removed during lookup")
    harness.background.completed(_part("A"), older)
    harness.builds.cache.latest_error = None
    worker.finish(_built("r1", "worker-old"))
    harness.builds.request(1).finish()
    snapshot = _until(stream, lambda snapshot: _ready(snapshot, "B"))
    assert _record(snapshot, "A")["build_id"] == "manual-new"
    assert next(stream) is None
