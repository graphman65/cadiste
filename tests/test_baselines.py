from __future__ import annotations

import json
from pathlib import Path

import pytest

from cadiste.baselines import Baselines
from cadiste.cache import TraceCache
from cadiste.server import Build, Builds, Project


def _target(name: str = "A") -> dict[str, str]:
    return {"kind": "part", "name": name}


class _Builds(Builds):
    def __init__(self, directory: Path):
        super().__init__(Project("model:build", (), ("A", "B")), TraceCache(directory, (), "baseline-tests"), False)
        self.count = 0

    def get(self, target, fresh=False):
        def make(shapes: Path):
            self.count += 1
            shapes.write_bytes(f"geometry-{self.count}".encode())
            return json.dumps(
                {
                    "build_id": shapes.name.split(".shapes.", 1)[0],
                    "source_revision": f"revision-{self.count}",
                    "target": target,
                }
            ).encode()

        body, cached, shapes = self.cache.get_or_make("trace", target["name"], make, fresh)
        return Build(body, cached, shapes, {})

    def latest(self, target, **options):
        file = self.cache.latest("trace", target["name"])
        return None if file is None else Build(file.read_bytes(), True, file.with_suffix(".shapes"), {})

    def captured(self, target, build_id):
        file = self.cache.captured("trace", target["name"], build_id)
        if file is None:
            raise LookupError("requested build is unavailable or expired")
        return Build(file.read_bytes(), True, file.with_suffix(".shapes"), {})


def test_multi_target_pin_survives_generation_eviction_warmup_and_store_restart(tmp_path: Path):
    builds = _Builds(tmp_path)
    first = builds.get(_target())
    second = builds.get(_target("B"))
    store = Baselines(builds)
    pinned = store.pin([_target(), _target("B")], "before edit")
    assert builds.count == 2  # Pin must not silently rebuild an already retained generation.
    for _ in range(10):
        builds.get(_target(), fresh=True)
        builds.get(_target("B"), fresh=True)
    assert not first.shapes.exists() and not second.shapes.exists()
    restarted = Baselines(builds)
    assert restarted.list() == [pinned]
    for target, original, geometry in ((_target(), first, b"geometry-1"), (_target("B"), second, b"geometry-2")):
        saved = restarted.get(pinned["id"], target)
        assert saved.body == original.body
        assert saved.shapes.read_bytes() == geometry


def test_pin_exact_displayed_generation_not_latest_warmed_generation(tmp_path: Path):
    builds = _Builds(tmp_path)
    displayed = builds.get(_target())
    latest = builds.get(_target(), fresh=True)
    store = Baselines(builds)
    displayed_id = json.loads(displayed.body)["build_id"]
    pinned = store.pin([{**_target(), "build_id": displayed_id}])
    assert store.get(pinned["id"], _target()).body == displayed.body
    assert pinned["targets"][0]["build_id"] != json.loads(latest.body)["build_id"]
    assert pinned["targets"][0]["target"] == _target()
    assert builds.count == 2


def test_same_label_never_overwrites_pinned_baseline_and_release_is_scoped(tmp_path: Path):
    builds = _Builds(tmp_path)
    store = Baselines(builds)
    first = store.pin([_target()], "reference")
    old = store.get(first["id"], _target())
    fresh = builds.get(_target(), fresh=True)
    second = store.pin([_target()], "reference")
    assert first["id"] != second["id"]
    assert store.get(first["id"], _target()).body == old.body
    assert store.get(second["id"], _target()).body == fresh.body
    assert store.release(first["id"])["released"]
    assert fresh.shapes.is_file()
    assert store.get(second["id"], _target()).shapes.is_file()
    with pytest.raises(LookupError, match="unavailable or released"):
        store.get(first["id"], _target())


def test_expired_generation_never_falls_back_to_latest(tmp_path: Path):
    builds = _Builds(tmp_path)
    original = builds.get(_target())
    identifier = json.loads(original.body)["build_id"]
    for _ in range(9):
        builds.get(_target(), fresh=True)
    store = Baselines(builds)
    with pytest.raises(LookupError, match="expired"):
        store.pin([{**_target(), "build_id": identifier}])
    assert store.list() == []
    assert not list(store.directory.glob(".*.tmp"))


def test_failed_target_does_not_publish_partial_target_set(tmp_path: Path, monkeypatch):
    builds = _Builds(tmp_path)
    builds.get(_target())
    monkeypatch.setattr(builds, "get", lambda target, **kwargs: Build(None, False, None, {"error": "bad model"}))
    store = Baselines(builds)
    with pytest.raises(ValueError, match="cannot pin failed target"):
        store.pin([_target(), _target("B")])
    assert store.list() == []


def test_missing_pinned_artifacts_and_targets_are_explicit(tmp_path: Path):
    store = Baselines(_Builds(tmp_path))
    pinned = store.pin([_target()])
    with pytest.raises(LookupError, match="does not contain target"):
        store.get(pinned["id"], _target("B"))
    saved = store.get(pinned["id"], _target())
    saved.shapes.unlink()
    with pytest.raises(LookupError, match="artifacts are unavailable"):
        store.get(pinned["id"], _target())
    with pytest.raises(LookupError, match="invalid baseline id"):
        store.release("../outside")


def test_conflicting_target_generations_are_rejected(tmp_path: Path):
    store = Baselines(_Builds(tmp_path))
    with pytest.raises(ValueError, match="two generations"):
        store.pin([_target(), {**_target(), "build_id": "other"}])
