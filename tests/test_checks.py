from __future__ import annotations

import math
from pathlib import Path

import pytest
from build123d import Box, Compound, Edge, Location

# OCP is a compiled extension without type stubs.
# pyright: reportAttributeAccessIssue=false
from OCP.TopoDS import TopoDS_Shape

from cadiste.checks import MAX_PAIRS, MAX_SAMPLES, check
from cadiste.measure import MeasureError, Shapes, load, save
from cadiste.mesh import SceneObject, tessellate


def _scene(tmp_path: Path, *objects: SceneObject):
    scene = tessellate(objects)
    file = tmp_path / "scene.shapes"
    save(scene.leaves, file)
    return scene, load(file)


@pytest.mark.parametrize(
    ("offset", "status", "volume", "distance", "passed"),
    [(1, "overlap", 4, 0, False), (2, "touching", 0, 0, True), (5, "separated", 0, 3, True)],
)
def test_exact_overlap_touching_and_separation(tmp_path: Path, offset, status, volume, distance, passed):
    scene, shapes = _scene(
        tmp_path,
        SceneObject("first", Box(2, 2, 2)),
        SceneObject("second", Box(2, 2, 2).moved(Location((offset, 0, 0)))),
    )
    answer = check(shapes, [[scene.paths["first"], scene.paths["second"]]])
    (pair,) = answer["pairs"]

    assert pair["available"]
    assert pair["status"] == status
    assert pair["overlap_volume"] == pytest.approx(volume)
    assert pair["minimum_distance"] == pytest.approx(distance)
    assert all(info["valid"] and info["solid_count"] == 1 for info in pair["validity"].values())
    assert math.dist(*pair["witnesses"]) == pytest.approx(distance)
    assert answer["ok"] is passed
    assert "penetration_depth" not in pair
    assert "does not measure penetration" in answer["scope"]["penetration_depth"]


def test_clearance_assertion_has_explicit_linear_tolerance(tmp_path: Path):
    scene, shapes = _scene(
        tmp_path,
        SceneObject("a", Box(2, 2, 2)),
        SceneObject("b", Box(2, 2, 2).moved(Location((5, 0, 0)))),
    )
    pairs = [[scene.paths["a"], scene.paths["b"]]]
    assert check(shapes, pairs, clearance=3 + 5e-7)["ok"]
    failure = check(shapes, pairs, clearance=3.01)
    assert not failure["ok"]
    assert failure["pairs"][0]["assertions"] == {"no_interference": True, "clearance": False}


def test_overlap_volume_tolerance_is_explicit_and_does_not_change_raw_volume(tmp_path: Path):
    scene, shapes = _scene(
        tmp_path,
        SceneObject("a", Box(2, 2, 2)),
        SceneObject("b", Box(2, 2, 2).moved(Location((1.99, 0, 0)))),
    )
    pairs = [[scene.paths["a"], scene.paths["b"]]]
    strict = check(shapes, pairs)
    allowed = check(shapes, pairs, volume_tolerance=0.05)

    assert strict["pairs"][0]["overlap_volume"] == pytest.approx(0.04)
    assert not strict["ok"]
    assert allowed["ok"]
    assert allowed["pairs"][0]["overlap_volume"] == pytest.approx(0.04)
    assert allowed["volume_tolerance"] == 0.05


def test_exclusions_are_unordered_explicit_pairs_and_do_not_scan_scene(tmp_path: Path):
    scene, shapes = _scene(
        tmp_path,
        SceneObject("a", Box(2, 2, 2)),
        SceneObject("b", Box(2, 2, 2)),
        SceneObject("unrequested", Box(2, 2, 2)),
    )
    a, b, other = (scene.paths[name] for name in ("a", "b", "unrequested"))
    answer = check(shapes, [[a, b]], ignore=[[b, a]])
    assert answer["ok"]
    assert answer["pairs"] == []
    assert answer["validity"] == {}
    assert answer["ignored"] == [[b, a]]
    with pytest.raises(MeasureError, match="only explicitly requested"):
        check(shapes, [[a, b]], ignore=[[a, other]])


def test_nested_rotated_assembly_checks_match_world_viewer_placement(tmp_path: Path):
    first = Box(2, 2, 2)
    first.label = "first"
    second = Box(2, 2, 2).moved(Location((5, 0, 0)))
    second.label = "second"
    subgroup = Compound(children=[first, second])
    subgroup.label = "subgroup"
    assembly = Compound(children=[subgroup]).moved(Location((100, 200, 30), (0, 0, 90)))
    scene, shapes = _scene(tmp_path, SceneObject("assembly", assembly))
    paths = {leaf.path.rsplit("/", 1)[-1]: leaf.path for leaf in scene.leaves}
    answer = check(shapes, [[paths["first"], paths["second"]]])
    (pair,) = answer["pairs"]

    assert pair["minimum_distance"] == pytest.approx(3)
    assert pair["overlap_volume"] == pytest.approx(0)
    assert math.dist(*pair["witnesses"]) == pytest.approx(3)
    for witness in pair["witnesses"]:
        assert 99 <= witness[0] <= 101
        assert 29 <= witness[2] <= 31
    assert sorted(point[1] for point in pair["witnesses"]) == pytest.approx([201, 204])


def test_pose_samples_are_independent_not_accumulated(tmp_path: Path):
    scene, shapes = _scene(
        tmp_path,
        SceneObject("moving", Box(2, 2, 2)),
        SceneObject("fixed", Box(2, 2, 2).moved(Location((5, 0, 0)))),
    )
    moving, fixed = scene.paths["moving"], scene.paths["fixed"]
    answer = check(
        shapes,
        [[moving, fixed]],
        samples=[
            {"path": moving, "translation": [3, 0, 0], "rotation": [0, 0, 0]},
            {"path": moving, "translation": [1, 0, 0], "rotation": [0, 0, 0]},
        ],
    )
    assert answer["pairs"][0]["minimum_distance"] == pytest.approx(3)
    assert [sample["pairs"][0]["minimum_distance"] for sample in answer["samples"]] == pytest.approx([0, 2])
    assert answer["scope"]["samples_accumulate"] is False
    assert check(shapes, [[moving, fixed]])["pairs"][0]["minimum_distance"] == pytest.approx(3)


@pytest.mark.parametrize(
    ("rotation", "fixed_at", "translation"),
    [
        ([0, 0, 90], (10, 3, 0), [0, 1, 0]),
        ([0, 90, 0], (10, 0, 3), [0, 0, 1]),
    ],
)
def test_rotation_uses_explicit_world_origin_and_independent_translation(
    tmp_path: Path, rotation, fixed_at, translation
):
    scene, shapes = _scene(
        tmp_path,
        SceneObject("bar", Box(4, 1, 1).moved(Location((10, 0, 0)))),
        SceneObject("fixed", Box(1, 1, 1).moved(Location(fixed_at))),
    )
    bar, fixed = scene.paths["bar"], scene.paths["fixed"]
    answer = check(
        shapes,
        [[bar, fixed]],
        samples=[
            {"path": bar, "translation": [0, 0, 0], "rotation": rotation, "origin": [10, 0, 0]},
            {"path": bar, "translation": translation, "rotation": rotation, "origin": [10, 0, 0]},
            {"path": bar, "translation": [0, 0, 0], "rotation": rotation, "origin": [10, 0, 0]},
        ],
    )
    assert answer["pairs"][0]["minimum_distance"] == pytest.approx(2)
    results = [sample["pairs"][0] for sample in answer["samples"]]
    assert [pair["minimum_distance"] for pair in results] == pytest.approx([0.5, 0, 0.5])
    assert [pair["overlap_volume"] for pair in results] == pytest.approx([0, 0.5, 0])
    assert [sample["origin"] for sample in answer["samples"]] == [[10, 0, 0]] * 3
    assert check(shapes, [[bar, fixed]])["pairs"][0]["minimum_distance"] == pytest.approx(2)


def test_omitted_sample_origin_uses_the_captured_world_placement(tmp_path: Path):
    scene, shapes = _scene(
        tmp_path,
        SceneObject("moving", Box(4, 1, 1).moved(Location((10, 0, 0)))),
        SceneObject("fixed", Box(1, 1, 1).moved(Location((10, 3, 0)))),
    )
    moving, fixed = scene.paths["moving"], scene.paths["fixed"]
    recorded = shapes.placements[moving].Transformation()
    origin = [recorded.Value(row, 4) for row in range(1, 4)]
    pose = {"path": moving, "translation": [0, 1, 0], "rotation": [0, 0, 90]}
    answer = check(shapes, [[moving, fixed]], samples=[pose, {**pose, "origin": origin}])
    implicit, explicit = (sample["pairs"][0] for sample in answer["samples"])

    assert implicit["available"] and explicit["available"]
    assert implicit["minimum_distance"] == pytest.approx(explicit["minimum_distance"])
    assert implicit["overlap_volume"] == pytest.approx(explicit["overlap_volume"])
    assert implicit["assertions"] == explicit["assertions"]


def test_noncontinuous_samples_make_no_claim_about_collision_between_poses(tmp_path: Path):
    scene, shapes = _scene(
        tmp_path,
        SceneObject("moving", Box(2, 2, 2).moved(Location((-4, 0, 0)))),
        SceneObject("fixed", Box(2, 2, 2)),
    )
    moving, fixed = scene.paths["moving"], scene.paths["fixed"]
    answer = check(
        shapes,
        [[moving, fixed]],
        samples=[
            {"path": moving, "translation": [8, 0, 0], "rotation": [0, 0, 0]},
        ],
    )
    assert answer["ok"]
    assert answer["scope"]["motion"] == "independent_samples_not_continuous"
    assert answer["samples"][0]["pairs"][0]["minimum_distance"] == pytest.approx(2)


def test_invalid_whole_shape_never_passes_assertions():
    paths = ["/assembly/a", "/assembly/b"]
    shapes = Shapes(
        {path: {"faces": [], "edges": [], "vertices": []} for path in paths},
        solids={path: TopoDS_Shape() for path in paths},
        whole_available=True,
    )
    answer = check(shapes, [paths])
    assert not answer["ok"]
    assert answer["pairs"][0]["status"] == "invalid_shape"
    assert answer["pairs"][0]["overlap_volume"] is None
    assert all(info["status"] == "null" and not info["valid"] for info in answer["validity"].values())


def test_non_solid_viewer_leaf_is_not_silently_reconstructed(tmp_path: Path):
    scene, shapes = _scene(
        tmp_path,
        SceneObject("solid", Box(2, 2, 2)),
        SceneObject("line", Edge.make_line((0, 0, 0), (1, 0, 0))),
    )
    with pytest.raises(MeasureError, match="no whole solid"):
        check(shapes, [[scene.paths["solid"], scene.paths["line"]]])


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, -1])
def test_clearance_requires_finite_nonnegative_number(value):
    with pytest.raises(MeasureError, match="clearance"):
        check(Shapes({}), [["a", "b"]], clearance=value)


@pytest.mark.parametrize("key", ["translation", "rotation", "origin"])
def test_sample_vectors_reject_nonfinite_values(key):
    sample = {"path": "a", "translation": [0, 0, 0], "rotation": [0, 0, 0]}
    sample[key] = [0, math.inf, 0]
    with pytest.raises(MeasureError, match="sample"):
        check(Shapes({}), [["a", "b"]], samples=[sample])


def test_requests_bound_pair_sample_and_total_evaluation_counts():
    with pytest.raises(MeasureError, match="explicit pairs"):
        check(Shapes({}), [[f"a{i}", f"b{i}"] for i in range(MAX_PAIRS + 1)])
    pose = {"path": "a", "translation": [0, 0, 0], "rotation": [0, 0, 0]}
    with pytest.raises(MeasureError, match="independent poses"):
        check(Shapes({}), [["a", "b"]], samples=[pose] * (MAX_SAMPLES + 1))
    with pytest.raises(MeasureError, match="pair/pose evaluations"):
        check(Shapes({}), [["a", f"b{i}"] for i in range(MAX_PAIRS)], samples=[pose] * MAX_SAMPLES)


def test_pair_paths_must_be_whole_leaves_and_unique(tmp_path: Path):
    scene, shapes = _scene(tmp_path, SceneObject("a", Box(2, 2, 2)), SceneObject("b", Box(2, 2, 2)))
    a, b = scene.paths["a"], scene.paths["b"]
    with pytest.raises(MeasureError, match="repeated unordered"):
        check(shapes, [[a, b], [b, a]])
    with pytest.raises(MeasureError, match="whole leaf instance"):
        check(shapes, [[a + "/faces/faces_0", b]])
    with pytest.raises(MeasureError, match=r"older shape cache.*rebuild"):
        check(Shapes(shapes.leaves), [[a, b]])


def test_coordinated_samples_move_joint_members_together_and_report_resolved_origins(tmp_path: Path):
    scene, shapes = _scene(
        tmp_path,
        SceneObject("first", Box(2, 2, 2)),
        SceneObject("second", Box(2, 2, 2).moved(Location((5, 0, 0)))),
    )
    first, second = scene.paths["first"], scene.paths["second"]
    poses = [
        {"path": path, "translation": [5, 0, 0], "rotation": [0, 0, 0]}
        for path in (first, second)
    ]
    independent = check(shapes, [[first, second]], samples=[poses[0]])
    together = check(shapes, [[first, second]], samples=[{"name": "joint motion", "poses": poses}])
    assert not independent["ok"]  # Sequential/single-member interpretation would collide.
    assert together["ok"]
    sample = together["samples"][0]
    assert sample["name"] == "joint motion"
    assert sample["pairs"][0]["minimum_distance"] == pytest.approx(3)
    assert all(len(pose["origin"]) == 3 for pose in sample["poses"])
    assert together["failing_poses"] == []
    failed = independent["failing_poses"][0]
    assert failed["poses"][0]["path"] == first
    assert failed["pairs"][0]["witnesses"]


def test_coordinated_pose_rejects_repeated_members_instead_of_composing_transforms():
    pose = {"path": "a", "translation": [1, 0, 0], "rotation": [0, 0, 0]}
    with pytest.raises(MeasureError, match="same path twice"):
        check(Shapes({}), [["a", "b"]], samples=[{"poses": [pose, pose]}])


def test_intentional_contact_is_asserted_not_ignored_and_does_not_hide_excess_overlap(tmp_path: Path):
    scene, shapes = _scene(
        tmp_path,
        SceneObject("a", Box(2, 2, 2)),
        SceneObject("b", Box(2, 2, 2).moved(Location((2, 0, 0)))),
    )
    a, b = scene.paths["a"], scene.paths["b"]
    contact = [{"paths": [a, b], "max_distance": 0.01, "max_overlap_volume": 0.001}]
    touching = check(shapes, [[a, b]], clearance=5, contact=contact)
    assert touching["ok"]  # This pair intentionally uses contact, not separation clearance.
    assert touching["pairs"][0]["assertions"]["intentional_contact"]
    separated = check(
        shapes, [[a, b]], contact=contact,
        samples=[{"path": b, "translation": [1, 0, 0], "rotation": [0, 0, 0]}],
    )
    assert not separated["ok"]
    assert separated["failing_poses"][0]["pairs"][0]["minimum_distance"] == pytest.approx(1)
    overlap = check(
        shapes, [[a, b]], contact=contact,
        samples=[{"path": b, "translation": [-1, 0, 0], "rotation": [0, 0, 0]}],
    )
    assert not overlap["ok"]
    assert overlap["failing_poses"][0]["pairs"][0]["overlap_volume"] == pytest.approx(4)


def test_intentional_press_fit_keeps_overlap_evidence_with_explicit_allowance(tmp_path: Path):
    scene, shapes = _scene(
        tmp_path,
        SceneObject("a", Box(2, 2, 2)),
        SceneObject("b", Box(2, 2, 2).moved(Location((1.99, 0, 0)))),
    )
    a, b = scene.paths["a"], scene.paths["b"]
    result = check(
        shapes, [[a, b]],
        contact=[{"paths": [a, b], "max_overlap_volume": 0.05, "max_distance": 0}],
    )
    assert result["ok"] and result["ignored"] == []
    row = result["pairs"][0]
    assert row["overlap_volume"] == pytest.approx(0.04)
    assert not row["assertions"]["no_interference"]
    assert row["assertions"]["intentional_contact"]
