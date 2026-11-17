from __future__ import annotations

from pathlib import Path

import pytest
from build123d import Box, Cylinder, GeomType, Location

from cadiste.measure import MeasureError, load, save
from cadiste.mechanical import run_assertion, run_check, travel_samples
from cadiste.mesh import SceneObject, tessellate


def _scene(directory: Path, **objects):
    directory.mkdir(parents=True, exist_ok=True)
    scene = tessellate([SceneObject(name, shape) for name, shape in objects.items()])
    archive = directory / "scene.shapes"
    save(scene.leaves, archive)
    return scene, load(archive)


def _cylinder_path(scene, name, shape):
    index = next(index for index, face in enumerate(shape.faces()) if face.geom_type == GeomType.CYLINDER)
    return f"{scene.paths[name]}/faces/faces_{index}"


def test_preserved_solid_checks_exact_interface_not_equal_volume_and_bounds(tmp_path: Path):
    before_shape = Box(10, 10, 10) - Cylinder(1, 20).moved(Location((-2, 0, 0)))
    after_shape = Box(10, 10, 10) - Cylinder(1, 20).moved(Location((2, 0, 0)))
    assert before_shape.volume == pytest.approx(after_shape.volume)
    assert before_shape.bounding_box().size == after_shape.bounding_box().size
    scene, before = _scene(tmp_path / "old", piece=before_shape)
    _, after = _scene(tmp_path / "new", piece=after_shape)
    request = {"kind": "preserved", "paths": [scene.paths["piece"]]}
    answer = run_assertion(after, request, before)
    assert not answer["ok"]
    assert answer["interfaces"][0]["difference_volumes"] == pytest.approx([10 * 3.141592653589793] * 2)
    _, same = _scene(tmp_path / "same", piece=before_shape)
    assert run_assertion(same, request, before)["ok"]


def test_preserved_trimmed_face_rejects_moved_equal_area_hole(tmp_path: Path):
    from cadiste.measure import _entity

    old_shape = Box(10, 10, 10) - Cylinder(1, 20).moved(Location((-2, 0, 0)))
    new_shape = Box(10, 10, 10) - Cylinder(1, 20).moved(Location((2, 0, 0)))
    scene, before = _scene(tmp_path / "old", piece=old_shape)
    _, after = _scene(tmp_path / "new", piece=new_shape)
    leaf = scene.paths["piece"]
    candidates = [f"{leaf}/faces/faces_{index}" for index in range(len(before.leaves[leaf]["faces"]))]
    path = next(
        path for path in candidates if (entity := _entity(before, path)).plane and abs(entity.plane[1][2]) > 0.9
    )
    assert not run_assertion(after, {"kind": "preserved", "paths": [path]}, before)["ok"]


def test_local_shape_preservation_does_not_mask_placement_change(tmp_path: Path):
    scene, before = _scene(tmp_path / "old", piece=Box(4, 2, 3))
    _, after = _scene(tmp_path / "new", piece=Box(4, 2, 3).moved(Location((10, 0, 0), (0, 0, 90))))
    path = scene.paths["piece"]
    request = {"kind": "preserved", "paths": [path]}
    assert not run_assertion(after, request, before)["ok"]
    assert run_assertion(after, {**request, "frame": "local"}, before)["ok"]
    placement = run_assertion(after, {"kind": "placement", "path": path}, before)
    assert not placement["ok"]
    _, translated = _scene(tmp_path / "translated", piece=Box(4, 2, 3).moved(Location((10, 0, 0))))
    translation = run_assertion(translated, {"kind": "placement", "path": path}, before)
    assert translation["translation_delta"] == pytest.approx([10, 0, 0])
    assert translation["angle_delta"] == pytest.approx(0)
    assert placement["angle_delta"] == pytest.approx(90)


def test_measurement_assertions_evaluate_exact_cylindrical_interface(tmp_path: Path):
    old, new = Cylinder(2, 10), Cylinder(3, 10)
    scene, before = _scene(tmp_path / "old", piece=old)
    _, after = _scene(tmp_path / "new", piece=new)
    path = _cylinder_path(scene, "piece", old)
    request = {"kind": "measurement", "paths": [path], "label": "Diameter", "preserved": True}
    answer = run_assertion(after, request, before)
    assert not answer["ok"]
    assert answer["quantity"]["value"] == pytest.approx(6)
    assert answer["delta"] == pytest.approx(2)
    assert run_assertion(after, {**request, "preserved": False, "min": 5.9, "max": 6.1})["ok"]
    with pytest.raises(MeasureError, match="baseline"):
        run_assertion(after, request)
    with pytest.raises(MeasureError, match="unavailable or ambiguous"):
        run_assertion(after, {**request, "label": "not a quantity"}, before)


@pytest.mark.parametrize("kind", ["insertion", "operating"])
def test_travel_samples_find_obstruction_between_endpoints_and_report_failure_pose(tmp_path: Path, kind):
    scene, shapes = _scene(
        tmp_path,
        moving=Box(2, 2, 2).moved(Location((-4, 0, 0))),
        fixed=Box(2, 2, 2),
    )
    moving, fixed = scene.paths["moving"], scene.paths["fixed"]
    answer = run_check(
        shapes,
        {
            "kind": kind,
            "pairs": [[moving, fixed]],
            "travel": {"steps": 3, "poses": [{"path": moving, "translation": [8, 0, 0], "rotation": [0, 0, 0]}]},
        },
    )
    assert not answer["ok"]
    assert [sample["passed"] for sample in answer["samples"]] == [True, False, True]
    failure = answer["failing_poses"][0]
    assert failure["fraction"] == pytest.approx(0.5)
    assert failure["poses"][0]["translation"] == pytest.approx([4, 0, 0])
    assert len(failure["poses"][0]["origin"]) == 3
    assert failure["pairs"][0]["witnesses"]
    assert "discrete" in answer["travel_evidence"]


def test_travel_interpolates_joint_members_in_one_simultaneous_pose(tmp_path: Path):
    scene, shapes = _scene(tmp_path, first=Box(2, 2, 2), second=Box(2, 2, 2).moved(Location((5, 0, 0))))
    first, second = scene.paths["first"], scene.paths["second"]
    result = run_check(
        shapes,
        {
            "kind": "operating",
            "pairs": [[first, second]],
            "travel": {
                "steps": 3,
                "poses": [{"path": path, "translation": [5, 0, 0], "rotation": [0, 0, 0]} for path in (first, second)],
            },
        },
    )
    assert result["ok"]
    assert all(sample["pairs"][0]["minimum_distance"] == pytest.approx(3) for sample in result["samples"])


@pytest.mark.parametrize("steps", [True, 1, 129, 2.5])
def test_travel_sampling_rejects_unbounded_or_unsupported_counts(steps):
    with pytest.raises(MeasureError, match="travel steps"):
        travel_samples({"steps": steps, "poses": []}, [["a", "b"]])


@pytest.mark.parametrize("kind", ["screw_engagement", "bearing_engagement"])
@pytest.mark.parametrize(("offset", "expected", "engaged"), [(4, True, 6), (9, False, 1)])
def test_engagement_uses_trimmed_axial_span_not_whole_cylinder_surface(tmp_path: Path, kind, offset, expected, engaged):
    shaft = Cylinder(2, 10).moved(Location((0, 0, offset)))
    bore = Cylinder(2.2, 10)
    scene, shapes = _scene(tmp_path, shaft=shaft, bore=bore)
    answer = run_check(
        shapes,
        {
            "kind": kind,
            "paths": [_cylinder_path(scene, "shaft", shaft), _cylinder_path(scene, "bore", bore)],
            "min_engagement": 5,
            "max_axis_distance": 0.01,
            "max_radial_clearance": 0.3,
        },
    )
    assert answer["ok"] is expected
    assert answer["engagement"] == pytest.approx(engaged)
    assert answer["radial_clearance"] == pytest.approx(0.2)
    assert answer["witnesses"][1][2] - answer["witnesses"][0][2] == pytest.approx(engaged)


def test_engagement_rejects_offset_axis_and_wrong_radial_fit(tmp_path: Path):
    shaft = Cylinder(2, 10)
    bore = Cylinder(2.2, 10).moved(Location((1, 0, 0)))
    scene, shapes = _scene(tmp_path, shaft=shaft, bore=bore)
    request = {
        "kind": "bearing_engagement",
        "paths": [_cylinder_path(scene, "shaft", shaft), _cylinder_path(scene, "bore", bore)],
        "min_engagement": 5,
        "max_axis_distance": 0.01,
        "max_radial_clearance": 0.1,
    }
    answer = run_check(shapes, request)
    assert not answer["ok"]
    assert not answer["assertions"]["axis_distance"]
    assert not answer["assertions"]["radial_fit"]
    with pytest.raises(MeasureError, match="unsupported"):
        run_check(shapes, {**request, "kind": "thread_strength"})
