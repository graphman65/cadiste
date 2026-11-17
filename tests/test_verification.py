from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from cadiste.cache import TraceCache
from cadiste.context import compare, structured_diff
from cadiste.server import Build, Builds, Project
from cadiste.verification import suggest_affected_targets, verify


def _target(name: str = "A", kind: str = "part") -> dict[str, str]:
    return {"kind": kind, "name": name}


def _leaf(name: str, x: float = 0) -> dict[str, Any]:
    return {"id": f"/Scene/{name}", "name": name, "loc": [[x, 0, 0], [0, 0, 0, 1]], "type": "shapes"}


def _part(x: float = 0, *, revision: str = "current") -> dict[str, Any]:
    return {
        "build_id": f"build-{x}-{revision}",
        "source_revision": revision,
        "source_fingerprint": "fingerprint",
        "sources": {"model.py": f"offset = {x}"},
        "build_seconds": 0,
        "result": {"faces": 6, "attributed": 0, "volume": 1000, "bbox": {"min": [x, 0, 0], "max": [x + 10, 10, 10]}},
        "ops": [],
        "builders": [],
        "scene": {"data": {"shapes": {"id": "/Scene", "parts": [_leaf("A", x)]}}},
    }


def _build(data: dict[str, Any] | None, *, shapes: Path | None = None, cached: bool = False) -> Build:
    return Build(
        None if data is None else json.dumps(data).encode(),
        cached,
        shapes,
        {"error": "model failed", "log": "build traceback"} if data is None else {},
    )


class _Builds(Builds):
    def __init__(self, directory: Path):
        project = Project("model:build", (), ("A", "B"), {"Main": "model:assembly"})
        super().__init__(project, TraceCache(directory, (), "tests"), False)
        self.previous: dict[str, Build] = {}
        self.outputs: dict[str, Build | Exception] = {}
        self.requests: list[tuple[dict[str, Any], bool]] = []
        self.latest_requests: list[dict[str, Any]] = []

    def get(self, target: dict[str, Any], fresh: bool) -> Build:
        self.requests.append((target, fresh))
        output = self.outputs.get(target.get("name", ""))
        if isinstance(output, Exception):
            raise output
        return output if output is not None else _build(_part())

    def latest(self, target: dict[str, Any]) -> Build | None:
        self.latest_requests.append(target)
        return self.previous.get(target.get("name", ""))


def test_pure_translation_is_visible_without_volume_or_size_change():
    before, after = _part(), _part(10)
    diff = structured_diff(before, after)
    assert diff["changed"]
    assert diff["bounds"]["before"]["min"] == [0, 0, 0]
    assert diff["bounds"]["after"]["min"] == [10, 0, 0]
    assert diff["bounds"]["translation"] == [10, 0, 0]
    assert diff["origin"]["delta"] == [10, 0, 0]
    report = compare(before, after)
    assert "volume: 1000.00 mm³ (unchanged)" in report
    assert "bounds translation: (10.000, 0.000, 0.000) mm" in report
    assert "part origin:" in report


def test_composite_reports_moved_added_and_removed_instances():
    before = {"scene": {"data": {"shapes": {"id": "/Scene", "parts": [_leaf("A"), _leaf("B")]}}}}
    after = {"scene": {"data": {"shapes": {"id": "/Scene", "parts": [_leaf("A", 10), _leaf("C")]}}}}
    diff = structured_diff(before, after)
    instances = diff["instances"]
    assert [item["path"] for item in instances["added"]] == ["/Scene/C"]
    assert [item["path"] for item in instances["removed"]] == ["/Scene/B"]
    assert instances["changed"][0]["translation_delta"] == [10, 0, 0]
    assert "~ instance /Scene/A" in compare(before, after)
    assert "+ instance /Scene/C" in compare(before, after)
    assert "- instance /Scene/B" in compare(before, after)


def test_inherited_rotation_places_local_offsets_in_world_coordinates():
    before = {"scene": {"data": {"shapes": {"id": "/Scene", "parts": [_leaf("A", 10)]}}}}
    after = copy.deepcopy(before)
    after["scene"]["data"]["shapes"]["loc"] = [[0, 0, 0], [0, 0, 2**-0.5, 2**-0.5]]
    changed = structured_diff(before, after)["instances"]["changed"][0]
    assert changed["after"]["transform"]["translation"] == pytest.approx([0, 10, 0])
    assert changed["translation_delta"] == pytest.approx([-10, 10, 0])


def test_scene_mesh_bounds_are_labeled_not_claimed_exact():
    before = {
        "scene": {
            "data": {
                "shapes": {
                    "id": "/Scene",
                    "parts": [],
                    "bb": {"xmin": 0, "xmax": 10, "ymin": 0, "ymax": 10, "zmin": 0, "zmax": 10},
                }
            }
        }
    }
    after = copy.deepcopy(before)
    after["scene"]["data"]["shapes"]["bb"].update(xmin=10, xmax=20)
    diff = structured_diff(before, after)
    assert diff["bounds"]["translation"] == [10, 0, 0]
    assert diff["bounds"]["after"]["basis"] == "viewer mesh bounds"


def test_target_failures_do_not_hide_other_successes(tmp_path: Path):
    builds = _Builds(tmp_path)
    builds.outputs["A"] = _build(None)
    builds.outputs["B"] = _build(_part(), cached=True)
    answer = verify(builds, [_target("A"), _target("B")])
    assert not answer["ok"]
    failed, succeeded = answer["results"]
    assert not failed["ok"] and failed["log"] == "build traceback"
    assert succeeded["ok"] and succeeded["cached"]
    assert succeeded["build_id"] == "build-0-current"
    assert succeeded["source_revision"] == "current"
    assert succeeded["source_fingerprint"] == "fingerprint"
    assert len(builds.requests) == 2


def test_raised_build_error_is_per_target(tmp_path: Path):
    builds = _Builds(tmp_path)
    builds.outputs["A"] = LookupError("missing target")
    answer = verify(builds, [_target("A"), _target("B")])
    assert answer["results"][0]["error"] == "missing target"
    assert answer["results"][1]["ok"]


def test_duplicate_targets_and_default_assembly_build_only_once(tmp_path: Path):
    builds = _Builds(tmp_path)
    answer = verify(builds, [{"kind": "assembly"}, _target("Main", "assembly"), _target(), _target()], fresh=True)
    assert answer["ok"]
    assert builds.requests == [(_target("Main", "assembly"), True), (_target(), True)]
    assert len(answer["results"]) == 2


def test_revision_mismatch_fails_explicitly_without_running_checks(tmp_path: Path):
    builds = _Builds(tmp_path)
    answer = verify(builds, [_target()], checks=[{"pairs": [["/A", "/B"]]}], expected_revision="stale")
    outcome = answer["results"][0]
    assert not answer["ok"] and not outcome["ok"]
    assert "source revision mismatch" in outcome["error"]
    assert "not run: source revision guard failed" in outcome["checks"][0]["error"]


def test_missing_revision_does_not_satisfy_guard(tmp_path: Path):
    builds = _Builds(tmp_path)
    data = _part()
    del data["source_revision"]
    builds.outputs["A"] = _build(data)
    outcome = verify(builds, [_target()], expected_revision="current")["results"][0]
    assert not outcome["ok"]
    assert "got None" in outcome["error"]


def test_requested_checks_use_exact_build_archive_and_do_not_rebuild(tmp_path: Path):
    def run_check(name: str, **arguments: Any) -> dict[str, Any]:
        assert name == "retained.shapes"
        calls.append(arguments)
        return {"ok": arguments.get("clearance", 0) == 0, "pairs": arguments["pairs"]}

    calls: list[dict[str, Any]] = []
    builds = _Builds(tmp_path)
    builds.check = run_check
    shapes = tmp_path / "retained.shapes"
    shapes.touch()
    builds.outputs["A"] = _build(_part(), shapes=shapes)
    checks = [
        {"target": _target(), "pairs": [["/A", "/B"]]},
        {"target": _target(), "pairs": [["/A", "/C"]], "clearance": 2},
    ]
    answer = verify(builds, [_target(), _target("B")], checks=checks)
    assert len(calls) == 2
    assert len(builds.requests) == 2
    assert not answer["results"][0]["ok"]
    assert answer["results"][1]["ok"]
    assert [check["ok"] for check in answer["results"][0]["checks"]] == [True, False]


def test_missing_shapes_fails_requested_checks_instead_of_skipping(tmp_path: Path):
    answer = verify(_Builds(tmp_path), [_target()], checks=[{"pairs": [["/A", "/B"]]}])
    outcome = answer["results"][0]
    assert not outcome["ok"]
    assert "retained shapes file" in outcome["checks"][0]["error"]


def test_unrequested_check_target_is_explicit_request_error(tmp_path: Path):
    builds = _Builds(tmp_path)
    with pytest.raises(ValueError, match="not in verification targets"):
        verify(builds, [_target()], checks=[{"target": _target("B"), "pairs": [["/A", "/B"]]}])
    assert builds.requests == []


def test_suggestions_retain_uncaptured_targets_and_never_build(tmp_path: Path):
    builds = _Builds(tmp_path)
    builds.previous["A"] = _build(_part())
    suggestions = suggest_affected_targets(builds, ["model.py"])
    assert not suggestions["complete"]
    assert builds.requests == []
    evidence, *conservative = suggestions["targets"]
    assert evidence["basis"] == "captured source dependency"
    assert evidence["files"] == ["model.py"]
    assert all(item["basis"] == "conservative candidate" for item in conservative)
    assert {item["target"]["name"] for item in conservative} == {"B", "Main"}


def test_verify_reports_changed_source_suggestions_without_building_other_targets(tmp_path: Path):
    builds = _Builds(tmp_path)
    builds.previous["A"] = _build(_part())
    builds.outputs["A"] = _build(_part(10))
    answer = verify(builds, [_target()])
    assert answer["ok"]
    assert answer["affected_targets"]["changed_files"] == ["model.py"]
    assert not answer["affected_targets"]["complete"]
    assert builds.requests == [(_target(), False)]
    assert answer["results"][0]["diff"]["bounds"]["translation"] == [10, 0, 0]


@pytest.mark.parametrize(("offset", "expected"), [(1, False), (5, True)])
def test_headless_checks_execute_exact_brep_archive_without_viewer(tmp_path: Path, offset: int, expected: bool):
    from build123d import Box, Location

    from cadiste.measure import save
    from cadiste.mesh import SceneObject, tessellate

    scene = tessellate(
        [
            SceneObject("first", Box(2, 2, 2)),
            SceneObject("second", Box(2, 2, 2).moved(Location((offset, 0, 0)))),
        ]
    )
    shapes = tmp_path / "actual.shapes"
    save(scene.leaves, shapes)
    builds = _Builds(tmp_path)
    builds.outputs["Main"] = _build(
        {
            "scene": {"data": scene.data, "paths": scene.paths},
            "source_revision": "current",
            "build_id": "actual",
            "build_seconds": 0,
        },
        shapes=shapes,
    )
    answer = verify(
        builds,
        [_target("Main", "assembly")],
        checks=[
            {
                "pairs": [[scene.paths["first"], scene.paths["second"]]],
            }
        ],
        expected_revision="current",
    )
    assert answer["ok"] is expected
    result = answer["results"][0]["checks"][0]["result"]
    assert result["pairs"][0]["overlap_volume"] == pytest.approx(4 if offset == 1 else 0)
    assert result["pairs"][0]["minimum_distance"] == pytest.approx(0 if offset == 1 else 3)
    assert builds.requests == [(_target("Main", "assembly"), False)]


def test_explicit_pinned_baseline_survives_latest_warmup_and_is_not_replaced_by_new_pin(tmp_path: Path):
    from cadiste.baselines import Baselines

    builds = _Builds(tmp_path)
    builds.previous["A"] = _build(_part(0, revision="original"))
    store = Baselines(builds)
    pinned = store.pin([_target()], "reference")
    builds.previous["A"] = _build(_part(10, revision="warmup"))
    newer = store.pin([_target()], "reference")
    builds.outputs["A"] = _build(_part(20, revision="edited"))
    builds.previous["A"] = builds.outputs["A"]  # The renderer already warmed the edited generation.
    outcome = verify(builds, [_target()], baseline=pinned["id"])["results"][0]
    assert outcome["ok"]
    assert outcome["comparison_basis"] == "pinned_baseline"
    assert outcome["baseline"]["build_id"] == "build-0-original"
    assert outcome["diff"]["bounds"]["translation"] == [20, 0, 0]
    assert store.get(newer["id"], _target()).body != store.get(pinned["id"], _target()).body
    assert builds.requests == [(_target(), False)]


def test_missing_baseline_target_fails_explicitly_without_hiding_independent_build(tmp_path: Path):
    from cadiste.baselines import Baselines

    builds = _Builds(tmp_path)
    builds.previous["A"] = _build(_part())
    pinned = Baselines(builds).pin([_target()])
    answer = verify(builds, [_target(), _target("B")], baseline=pinned["id"])
    first, second = answer["results"]
    assert first["ok"]
    assert not second["ok"]
    assert second["build_id"] == "build-0-current"
    assert not second["baseline"]["available"]
    assert "does not contain target" in second["comparison_error"]


def _exact_build(directory: Path, **objects):
    from cadiste.measure import save
    from cadiste.mesh import SceneObject, tessellate

    directory.mkdir(parents=True, exist_ok=True)
    scene = tessellate([SceneObject(name, shape) for name, shape in objects.items()])
    archive = directory / "scene.shapes"
    save(scene.leaves, archive)
    return scene, _build({
        "scene": {"data": scene.data, "paths": scene.paths},
        "source_revision": directory.name, "build_id": directory.name, "build_seconds": 0,
    }, shapes=archive)


def test_preserved_interface_assertion_uses_pinned_exact_shapes_after_source_archive_eviction(tmp_path: Path):
    from build123d import Box, Cylinder, Location

    from cadiste.baselines import Baselines

    scene, old = _exact_build(
        tmp_path / "old", piece=Box(10, 10, 10) - Cylinder(1, 20).moved(Location((-2, 0, 0))),
    )
    _, changed = _exact_build(
        tmp_path / "changed", piece=Box(10, 10, 10) - Cylinder(1, 20).moved(Location((2, 0, 0))),
    )
    builds = _Builds(tmp_path / "cache")
    builds.previous["A"] = old
    pinned = Baselines(builds).pin([_target()])
    old.shapes.unlink()
    builds.previous["A"] = changed
    builds.outputs["A"] = changed
    outcome = verify(builds, [_target()], baseline=pinned["id"], assertions=[
        {"kind": "preserved", "paths": [scene.paths["piece"]]},
    ])["results"][0]
    assert not outcome["ok"]
    assert outcome["baseline"]["shapes_available"]
    assertion = outcome["assertions"][0]
    assert not assertion["ok"] and "error" not in assertion
    assert all(value > 30 for value in assertion["result"]["interfaces"][0]["difference_volumes"])


def test_named_mechanical_check_set_runs_real_joint_travel_and_names_evidence(tmp_path: Path):
    from build123d import Box, Location

    scene, build = _exact_build(
        tmp_path / "assembly", first=Box(2, 2, 2), second=Box(2, 2, 2).moved(Location((5, 0, 0))),
    )
    first, second = scene.paths["first"], scene.paths["second"]
    builds = _Builds(tmp_path / "cache")
    builds.outputs["Main"] = build
    builds.project.check_sets = {
        "joint-travel": {
            "target": _target("Main", "assembly"),
            "checks": [{
                "kind": "operating", "pairs": [[first, second]],
                "travel": {"steps": 3, "poses": [
                    {"path": path, "translation": [5, 0, 0], "rotation": [0, 0, 0]}
                    for path in (first, second)
                ]},
            }],
        },
    }
    outcome = verify(builds, [_target("Main", "assembly")], check_sets=["joint-travel"])["results"][0]
    assert outcome["ok"]
    assert outcome["check_sets"] == [{"name": "joint-travel", "target": _target("Main", "assembly")}]
    assert outcome["checks"][0]["check_set"] == "joint-travel"
    assert len(outcome["checks"][0]["result"]["samples"][1]["poses"]) == 2
    assert all(pose["origin"] is not None for pose in outcome["checks"][0]["result"]["samples"][1]["poses"])


def test_variant_qualified_check_sets_use_effective_project_and_keep_local_slashes(tmp_path: Path):
    from build123d import Box, Location

    scene, build = _exact_build(
        tmp_path / "assembly", first=Box(2, 2, 2), second=Box(2, 2, 2).moved(Location((5, 0, 0))),
    )
    pairs = [[scene.paths["first"], scene.paths["second"]]]
    small = Project("model:build", (), ("A",), {"Main": "model:assembly"})
    large = Project("model:build", (), ("A",), {"Main": "model:assembly"})
    for project, clearance in ((small, 2), (large, 4)):
        project.check_sets = {
            "fit/path": {"target": _target("Main", "assembly"), "checks": [{"pairs": pairs, "clearance": clearance}]},
        }
    builds = _Builds(tmp_path / "cache")
    builds.project = Project("model:build", (), (), variants={"small": small, "large": large})
    builds.outputs = {"small/Main": build, "large/Main": build}
    qualified = verify(
        builds, [_target("small/Main", "assembly")], check_sets=["small/fit/path"],
    )["results"][0]
    assert qualified["ok"]
    assert qualified["checks"][0]["check_set"] == "small/fit/path"
    answer = verify(
        builds, [_target("small/Main", "assembly"), _target("large/Main", "assembly")], check_sets=["fit/path"],
    )
    assert [result["ok"] for result in answer["results"]] == [True, False]
    assert answer["results"][1]["checks"][0]["check_set"] == "large/fit/path"


def test_unknown_and_untargeted_check_sets_fail_before_building(tmp_path: Path):
    builds = _Builds(tmp_path)
    with pytest.raises(LookupError, match="unknown mechanical check set"):
        verify(builds, [_target()], check_sets=["missing"])
    builds.project.check_sets = {"other": {"target": _target("B"), "checks": [{"pairs": [["a", "b"]]}]}}
    with pytest.raises(ValueError, match="not in verification targets"):
        verify(builds, [_target()], check_sets=["other"])
    assert builds.requests == []


def test_total_verification_computation_is_bounded_across_checks(tmp_path: Path):
    builds = _Builds(tmp_path)
    with pytest.raises(ValueError, match="4096 exact"):
        verify(builds, [_target()], checks=[
            {"pairs": [["a", f"b{index}"] for index in range(128)]} for _ in range(33)
        ])
    assert builds.requests == []
