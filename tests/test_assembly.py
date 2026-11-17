from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
from build123d import Box, Compound, Location

from cadiste.assembly import capture_placements, describe_assembly, instance, match_parts, owning_part
from cadiste.export import assembly_scene
from cadiste.references import Snapshot


def test_fresh_equal_shapes_explicitly_owned_and_repeated_instances_distinct():
    first = instance(Box(2, 3, 4), part="Block", label="same", location=Location((0, 0, 0)))
    second = instance(Box(2, 3, 4), part="Block", label="same", location=Location((10, 0, 0)))
    scene, leaves = assembly_scene(Compound(label="Assembly", children=[first, second]))
    assert len(leaves) == 2 and len({leaf.path for leaf in leaves}) == 2
    assert list(scene["part_of"].values()) == ["Block", "Block"]
    assert scene["ownership"]["matched"] == scene["ownership"]["total"] == 2
    assert all(record["ownership"] == "explicit" for record in scene["instances"].values())
    assert all(leaf.shape is not None for leaf in leaves)
    assert leaves[1].location is not None
    # Rebuilding an equal canonical part neither erases nor establishes ownership.
    match_parts(scene, leaves, {"Block": Box(2, 3, 4)})
    assert scene["ownership"]["status"] == "complete"


def test_undefined_empty_and_partial_ownership_are_explained():
    scene, leaves = assembly_scene(Compound(label="Assembly", children=[Box(2, 3, 4)]))
    assert scene["ownership"]["status"] == "undefined"
    assert "configure parts" in scene["ownership"]["explanation"]
    match_parts(scene, leaves, {"Block": Box(2, 3, 4)})
    assert scene["ownership"]["status"] == "empty"
    assert scene["ownership"]["unmatched"] == [leaf.path for leaf in leaves]
    assert "Freshly rebuilt" in scene["ownership"]["explanation"]
    scene, leaves = assembly_scene(
        Compound(
            label="Assembly",
            children=[
                instance(Box(2, 3, 4), part="Block", label="owned"),
                Box(1, 1, 1),
            ],
        )
    )
    assert scene["ownership"]["status"] == "partial"
    assert scene["ownership"]["matched"] == 1 and scene["ownership"]["total"] == 2


def test_shared_topology_matching_remains_supported_but_ambiguous_catalog_is_not_owned():
    canonical = Box(2, 3, 4)
    placed = copy.copy(canonical).move(Location((10, 0, 0)))
    scene, leaves = assembly_scene(Compound(label="Assembly", children=[placed]))
    assert match_parts(scene, leaves, {"Block": canonical}) == {leaves[0].path: "Block"}
    assert scene["instances"][leaves[0].path]["ownership"] == "shared_topology"
    scene, leaves = assembly_scene(Compound(label="Assembly", children=[placed]))
    assert match_parts(scene, leaves, {"Block": canonical, "Alias": canonical}) == {}


def test_matching_labels_are_never_ownership_evidence():
    child = Box(2, 3, 4)
    child.label = "Block"
    scene, leaves = assembly_scene(Compound(label="Block", children=[child]))
    assert owning_part(scene, leaves[0].path, {"Block"}) is None
    scene["part_of"][leaves[0].path] = "Other"
    assert owning_part(scene, leaves[0].path, {"Block"}) is None
    assert owning_part(scene, leaves[0].path, {"Other"}) == "Other"


def test_captured_ordinary_placement_source_arguments_and_headless_pagination(tmp_path: Path):
    source = """from build123d import Box, Compound, Location
from cadiste.assembly import instance

def build():
    a = instance(Box(2, 3, 4), part="Block", label="first")
    a = a.moved(Location((12, 0, 0)))
    b = instance(Box(2, 3, 4), part="Block", label="second")
    return Compound(label="Assembly", children=[a, b])
"""
    file = tmp_path / "model.py"
    file.write_text(source)
    namespace = {"__name__": "model"}
    exec(compile(source, str(file), "exec"), namespace)
    previous = sys.getprofile()
    with capture_placements(root=tmp_path):
        result = namespace["build"]()
    assert sys.getprofile() is previous
    scene, _ = assembly_scene(result, root=tmp_path)
    built = {"scene": scene, "build_id": "exact-build", "build_seconds": 0}
    page = describe_assembly(built, limit=1)
    assert page["total"] == 2 and page["next_offset"] == 1
    assert page["build_id"] == "exact-build"
    placement = page["instances"][0]["placement"]
    assert placement["method"] == "moved"
    assert placement["code"] == "a.moved(Location((12, 0, 0)))"
    assert placement["definition"] == "code:model.py::build"
    assert placement["reference"].startswith("code:model.py::build::placement:")
    assert placement["arguments"][0]["value"]["position"] == [12, 0, 0]
    assert page["sources"]["model.py"] == source
    assert page["construction"]
    construction = next(iter(page["construction"].values()))
    assert construction["code"] == 'Compound(label="Assembly", children=[a, b])'
    assert page["instances"][0]["owner"] == "Block"
    assert len(page["instances"][0]["transform_chain"]) >= 2
    second = describe_assembly(built, offset=1, limit=1)
    assert second["next_offset"] is None
    assert second["instances"][0]["path"] != page["instances"][0]["path"]
    with pytest.raises(LookupError, match="not in this captured build"):
        describe_assembly(built, "/missing")
    with pytest.raises(ValueError):
        describe_assembly(built, limit=0)


def test_snapshot_separates_owner_from_actual_placement_source():
    placed = instance(Box(2, 3, 4), part="Block", label="placed", location=Location((5, 0, 0)))
    scene, _ = assembly_scene(Compound(label="Assembly", children=[placed]))
    lines = Snapshot._placement_lines({"scene": scene}, scene["data"]["shapes"])
    text = "\n".join(lines)
    assert "Recorded scene-chain placement" in text
    assert "Instance placement source: code:" in text
    assert "Placement owner definition (not owning-part definition)" in text
    assert "Captured placement call:" in text and "instance(Box" in text
    assert "Captured placement source excerpt:" in text


def test_placement_capture_restores_profiler_on_build_error(tmp_path: Path):
    previous = sys.getprofile()
    with pytest.raises(RuntimeError, match="model failed"):
        with capture_placements(root=tmp_path):
            raise RuntimeError("model failed")
    assert sys.getprofile() is previous
