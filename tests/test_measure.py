from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from build123d import Axis, Box, BuildPart, Cylinder, Edge, GeomType, Location, Mode, Part

from cadiste.measure import MeasureError, Shapes, load, measure, save
from cadiste.mesh import Scene, SceneObject, tessellate


def _drilled_block() -> Part:
    with BuildPart() as block:
        Box(10, 10, 10)
        Cylinder(2, 20, mode=Mode.SUBTRACT)
    assert block.part is not None
    return block.part


def _saved(tmp_path: Path, *objects: SceneObject) -> tuple[Scene, Shapes]:
    """The scene as the viewer gets it, and its shapes as the server reads them back."""
    scene = tessellate(objects)
    file = tmp_path / "scene.shapes"
    save(scene.leaves, file)
    return scene, load(file)


def _path(scene: Scene, name: str, topo: str, index: int) -> str:
    return f"{scene.paths[name]}/{topo}/{topo}_{index}"


def _rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {row["label"]: row["value"] for row in rows}


def test_a_hole_measures_its_exact_diameter_through_the_viewer_path(tmp_path: Path):
    block = _drilled_block()
    scene, shapes = _saved(tmp_path, SceneObject("block", block))
    (hole,) = [i for i, face in enumerate(block.faces()) if face.geom_type == GeomType.CYLINDER]

    (entity,) = measure(shapes, [_path(scene, "block", "faces", hole)])["entities"]

    assert entity["kind"] == "cylinder"
    properties = _rows(entity["properties"])
    assert properties["Diameter"] == pytest.approx(4, abs=1e-12)  # the mesh's polygon would fall short of it
    assert properties["Area"] == pytest.approx(2 * math.pi * 2 * 10)


def test_opposite_faces_measure_their_distance_and_angle_without_repeating_it(tmp_path: Path):
    block = _drilled_block()
    scene, shapes = _saved(tmp_path, SceneObject("block", block))
    faces = block.faces()
    left, right = (faces.index(faces.sort_by(Axis.X)[i]) for i in (0, -1))

    results = _rows(measure(shapes, [_path(scene, "block", "faces", i) for i in (left, right)])["results"])

    assert results["Minimum distance"] == pytest.approx(10)
    assert results["Angle"] == pytest.approx(0)
    assert "Normal distance" not in results  # the parallel faces face each other: it is the minimum distance


def test_instances_of_one_shape_are_measured_where_the_viewer_places_each(tmp_path: Path):
    block = _drilled_block()
    # One shape twice: ocp_tessellate meshes it once and places each instance by its location.
    scene, shapes = _saved(
        tmp_path,
        SceneObject("near", block),
        SceneObject("far", block.moved(Location((30, 0, 0), (0, 0, 90)))),
    )
    faces = block.faces()
    near_right = faces.index(faces.sort_by(Axis.X)[-1])
    far_faces = block.moved(Location((30, 0, 0), (0, 0, 90))).faces()
    far_left = far_faces.index(far_faces.sort_by(Axis.X)[0])

    paths = [_path(scene, "near", "faces", near_right), _path(scene, "far", "faces", far_left)]
    results = _rows(measure(shapes, paths)["results"])

    assert results["Minimum distance"] == pytest.approx(20)


def test_a_circle_edge_and_a_straight_edge_measure_the_distance_from_its_centre(tmp_path: Path):
    block = _drilled_block()
    scene, shapes = _saved(tmp_path, SceneObject("block", block))
    edges = block.edges()
    (rim,) = [i for i, e in enumerate(edges) if e.geom_type == GeomType.CIRCLE and e.center().Z > 0]
    (corner,) = [i for i, e in enumerate(edges) if e.geom_type == GeomType.LINE and tuple(e.center()) == (5, 5, 0)]

    answer = measure(shapes, [_path(scene, "block", "edges", rim), _path(scene, "block", "edges", corner)])

    rim_properties = _rows(answer["entities"][0]["properties"])
    assert rim_properties["Diameter"] == pytest.approx(4)
    assert rim_properties["Center"] == pytest.approx((0, 0, 5))
    results = _rows(answer["results"])
    assert results["Perpendicular distance"] == pytest.approx(math.sqrt(50))
    assert results["Minimum distance"] == pytest.approx(math.sqrt(50) - 2)


def test_a_line_drawn_on_its_own_is_numbered_as_the_viewer_numbers_its_edges(tmp_path: Path):
    scene, shapes = _saved(tmp_path, SceneObject("line", Edge.make_line((0, 0, 0), (3, 4, 0))))

    (entity,) = measure(shapes, [_path(scene, "line", "edges", 0)])["entities"]
    assert _rows(entity["properties"])["Length"] == pytest.approx(5)
    with pytest.raises(MeasureError):
        measure(shapes, [_path(scene, "line", "edges", 1)])
