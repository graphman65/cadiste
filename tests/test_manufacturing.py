from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from build123d import Box, Compound, Location

from cadiste.bambu_verify import read_geometry
from cadiste.manufacturing import _mesh_report, orient_geometry, preflight, prepare_export
from cadiste.measure import MeasureError, save
from cadiste.mesh import Leaf
from cadiste.three_mf import export_3mf


def _builds(tmp_path: Path, config=None, *, owners=None, kind="assembly"):
    first = Box(2, 4, 6).moved(Location((10, 20, 30)))
    second = Box(3, 5, 7).moved(Location((30, 20, 30)))
    file = tmp_path / "retained.shapes"
    save(
        [Leaf("/Assembly/first", [], [], [], first.wrapped), Leaf("/Assembly/second", [], [], [], second.wrapped)], file
    )
    data = {
        "build_id": "generation-1",
        "source_revision": "revision-old",
        "source_fingerprint": "fingerprint-old",
        "scene": {
            "data": {"shapes": {"id": "/Assembly"}},
            "part_of": owners or {"/Assembly/first": "First", "/Assembly/second": "Second"},
        },
    }
    build = SimpleNamespace(body=json.dumps(data).encode(), shapes=file)
    project = SimpleNamespace(manufacturing=config or {})
    project.resolve = lambda target: (project, target, None)
    calls = []

    def captured(target, build_id):
        calls.append(build_id)
        if build_id != "generation-1":
            raise LookupError("generation unavailable")
        return build

    builds = SimpleNamespace(
        project=project, latest=lambda target: build, captured=captured, geometry_lock=threading.RLock()
    )
    return builds, {"kind": kind, "name": "Assembly"}, file, calls


def test_retained_preflight_and_export_have_identical_world_geometry_without_source_mutation(tmp_path: Path):
    builds, target, file, calls = _builds(tmp_path)
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    prepared = prepare_export(builds, target, build_id="generation-1")
    output = tmp_path / "print.3mf"
    export_3mf(prepared.shape, output)
    imported = read_geometry(output)
    assert calls == ["generation-1"]
    assert imported["object_count"] == prepared.report["object_count"] == 2
    for actual, expected in zip(imported["objects"], prepared.report["objects"], strict=True):
        assert actual["min"] == pytest.approx(expected["bounds_mm"]["min"])
        assert actual["dimensions"] == pytest.approx(expected["bounds_mm"]["dimensions"])
        assert expected["mesh"]["closed_oriented_manifold"]
    assert prepared.report["source_revision"] == "revision-old"
    assert prepared.report["source_fingerprint"] == "fingerprint-old"
    assert prepared.report["evidence"] == "non_slicer_geometric_estimate"
    assert hashlib.sha256(file.read_bytes()).hexdigest() == digest


def test_registered_relative_group_rotation_on_bed_and_bed_envelope(tmp_path: Path):
    config = {
        "orientations": {"flat": {"rotation": [90, 0, 0], "translation": [0, 40, 0], "on_bed": True}},
        "groups": {"first-only": ["first"]},
        "bed": {"size": [100, 100, 100]},
    }
    builds, target, _, _ = _builds(tmp_path, config)
    report = preflight(builds, target, group="first-only", orientation="flat")
    assert report["object_count"] == 1
    obj = report["objects"][0]
    assert obj["path"] == "/Assembly/first"
    assert obj["bounds_mm"]["dimensions"] == pytest.approx([2, 6, 4])
    assert obj["bed_placement"] == {"on_z0": True, "below_z0": False, "within_registered_bed": True}
    assert obj["mesh"]["overhang"]["area_mm2"] == pytest.approx(0)
    assert obj["mesh"]["bridge_candidates"]["patch_count"] == 0


def test_per_part_orientation_applies_only_captured_owner_and_explicit_override_wins(tmp_path: Path):
    config = {
        "orientations": {"side": {"rotation": [90, 0, 0]}, "upright": {"rotation": [0, 0, 0]}},
        "part_orientations": {"First": "side"},
    }
    builds, target, _, _ = _builds(tmp_path, config)
    report = preflight(builds, target)
    assert report["objects"][0]["bounds_mm"]["dimensions"] == pytest.approx([2, 6, 4])
    assert report["objects"][1]["bounds_mm"]["dimensions"] == pytest.approx([3, 5, 7])
    override = preflight(builds, target, orientation="upright")
    assert override["objects"][0]["bounds_mm"]["dimensions"] == pytest.approx([2, 4, 6])


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"orientation": "missing"}, "unknown registered print orientation"),
        ({"group": "missing"}, "unknown registered export group"),
        ({"hidden": ["/Assembly/nope"]}, "not in this retained"),
        ({"hidden": ["/Assembly"]}, "nothing to export"),
        ({"expected_revision": "new"}, "revision mismatch"),
        ({"overhang_angle": float("nan")}, "finite"),
        ({"overhang_angle": True}, "finite"),
        ({"overhang_angle": 90}, "less than 90"),
        ({"only": ["First"]}, "folder part-name"),
    ],
)
def test_invalid_requests_never_silently_fall_back(tmp_path: Path, kwargs, match):
    builds, target, _, _ = _builds(tmp_path)
    with pytest.raises(ValueError, match=match):
        preflight(builds, target, **kwargs)


def test_alias_registration_is_validated_even_when_alias_would_not_match_selected_objects(tmp_path: Path):
    builds, target, _, _ = _builds(tmp_path, {"part_orientations": {"Unbuilt": "typo"}})
    with pytest.raises(ValueError, match="unknown registered print orientation"):
        preflight(builds, target)


def test_folder_allowlist_uses_exact_captured_qualified_owners(tmp_path: Path):
    builds, _, _, _ = _builds(tmp_path, owners={"/Assembly/first": "v/First", "/Assembly/second": "v/Second"})
    report = preflight(builds, {"kind": "file", "path": "v/folder"}, only=["v/First"])
    assert report["object_count"] == 1
    assert report["objects"][0]["owner"] == "v/First"
    with pytest.raises(ValueError, match="not captured"):
        preflight(builds, {"kind": "file", "path": "v/folder"}, only=["First"])


def test_missing_retained_whole_geometry_never_rebuilds(tmp_path: Path):
    builds, target, _, _ = _builds(tmp_path)
    builds.latest = lambda target: None
    with pytest.raises(MeasureError, match="build/show"):
        preflight(builds, target)


def test_orientation_helper_preserves_nested_object_boundaries_and_source_placements(tmp_path: Path):
    first = Box(2, 4, 6)
    first.label = "first"
    second = Box(2, 4, 6).moved(Location((10, 0, 0)))
    second.label = "second"
    nested = Compound(label="nested", children=[first, second]).moved(Location((0, 5, 0)))
    assembly = Compound(label="assembly", children=[nested]).moved(Location((20, 0, 0)))
    location = assembly.location
    result = orient_geometry(assembly, [0, 0, 90], [100, 0, 0], on_bed=True)
    output = tmp_path / "oriented.3mf"
    export_3mf(result, output)
    geometry = read_geometry(output)
    assert geometry["object_count"] == 2
    assert geometry["objects"][0]["dimensions"] == pytest.approx([4, 2, 6])
    assert geometry["objects"][1]["min"][1] - geometry["objects"][0]["min"][1] == pytest.approx(10)
    assert min(obj["min"][2] for obj in geometry["objects"]) == pytest.approx(0)
    assert assembly.location == location
    assert len(assembly.children[0].children) == 2


def test_mesh_diagnostics_detect_open_surface_winding_duplicates_and_vertex_bowtie():
    vertices = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    tetrahedron = [(0, 2, 1), (0, 1, 3), (1, 2, 3), (2, 0, 3)]
    assert _mesh_report(vertices, tetrahedron, 45)["closed_oriented_manifold"]
    assert _mesh_report(vertices, tetrahedron[:-1], 45)["boundary_edges"] == 3
    flipped = [*tetrahedron[:3], (2, 3, 0)]
    assert _mesh_report(vertices, flipped, 45)["inconsistent_winding_edges"] == 3
    duplicate = _mesh_report(vertices, tetrahedron + tetrahedron[:1], 45)
    assert duplicate["duplicate_triangles"] == 1
    # Two closed shells sharing only vertex zero are not a manifold at that vertex.
    bowtie = [*vertices, (-1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0)]
    triangles = [*tetrahedron, (0, 5, 4), (0, 4, 6), (4, 5, 6), (5, 0, 6)]
    result = _mesh_report(bowtie, triangles, 45)
    assert result["nonmanifold_edges"] == 0
    assert result["nonmanifold_vertices"] == 1
    assert not result["closed_oriented_manifold"]


def test_disabling_analysis_keeps_exact_selection_dimensions_and_bed_without_meshing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    builds, target, _, _ = _builds(tmp_path, {"groups": {"single": ["/Assembly/second"]}})

    def unexpected_mesh(*args, **kwargs):
        pytest.fail("analysis=False must not tessellate")

    monkeypatch.setattr("cadiste.manufacturing.mesh_geometry", unexpected_mesh)
    prepared = prepare_export(builds, target, group="single", analyze=False)
    assert prepared.report["object_count"] == 1
    assert prepared.report["bounds_mm"]["dimensions"] == pytest.approx([3, 5, 7])
    assert prepared.report["objects"][0]["mesh"] is None
    assert prepared.report["objects"][0]["brep_valid"] is None
    assert not prepared.report["analysis_performed"]
    assert prepared.report["source_revision"] == "revision-old"
    with pytest.raises(ValueError, match="unknown registered"):
        prepare_export(builds, target, group="typo", analyze=False)


def test_geometry_loading_and_analysis_hold_the_shared_measurement_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from cadiste import manufacturing

    builds, target, _, _ = _builds(tmp_path)
    held = []

    class Lock:
        def __enter__(self):
            held.append(True)

        def __exit__(self, *args):
            held.pop()

    original_load = manufacturing.load

    def guarded_load(path):
        assert held
        return original_load(path)

    builds.geometry_lock = Lock()
    monkeypatch.setattr(manufacturing, "load", guarded_load)
    assert preflight(builds, target)["object_count"] == 2
    assert held == []


def test_part_preflight_exports_only_captured_final_part_not_construction_geometry(tmp_path: Path):
    builds, _, _, _ = _builds(tmp_path)
    build = builds.latest({})
    metadata = json.loads(build.body)
    metadata["scene"]["part"] = "/Assembly/first"
    build.body = json.dumps(metadata).encode()
    report = preflight(builds, {"kind": "part", "name": "First"})
    assert report["object_count"] == 1
    assert report["objects"][0]["path"] == "/Assembly/first"
    assert report["bounds_mm"]["dimensions"] == pytest.approx([2, 4, 6])


def test_retained_export_preserves_nested_group_labels_and_repeated_source_names(tmp_path: Path):
    from cadiste.export import assembly_scene

    first = Box(2, 4, 6)
    first.label = "cube"
    second = Box(2, 4, 6).moved(Location((30, 0, 0)))
    second.label = "cube"
    assembly = Compound(label="Assembly", children=[Compound(label="Pair", children=[first, second])])
    scene, leaves = assembly_scene(assembly)
    builds, target, file, _ = _builds(tmp_path)
    save(leaves, file)
    build = builds.latest(target)
    metadata = json.loads(build.body)
    metadata["scene"] = scene
    build.body = json.dumps(metadata).encode()
    prepared = prepare_export(builds, target, analyze=False)
    assert prepared.shape.label == "Assembly"
    pair = prepared.shape.children[0]
    assert pair.label == "Pair"
    assert [child.label for child in pair.children] == ["cube", "cube"]
    assert sorted(child.bounding_box().min.X for child in pair.children) == pytest.approx([-1, 29])
    assert prepared.report["object_count"] == 2
