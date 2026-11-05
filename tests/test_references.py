from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from build123d import Axis, Box, BuildPart, Cylinder, Location, Locations, Mode, Part, fillet

from cadiste import Provenance, context, tracing
from cadiste.agent import Agent
from cadiste.export import export_trace
from cadiste.hub import Hub
from cadiste.measure import Shapes, adjacent_faces, corresponding_entity, load, save
from cadiste.mesh import SceneObject, tessellate
from cadiste.references import SelectionItem, Snapshot, SnapshotError, selection_items
from cadiste.server import Build, Builds, Measurer, Project


class _Builds:
    def __init__(self):
        self.entries: dict[str, Build] = {}

    def put(self, target: dict[str, Any], build: Build) -> None:
        self.entries[json.dumps(target, sort_keys=True)] = build

    def latest(self, target: dict[str, Any]) -> Build | None:
        return self.entries.get(json.dumps(target, sort_keys=True))


def _independent_part(hole: float, *, fillets: bool = False) -> Part:
    """A separate untraced execution, as the folder's model cache supplies."""
    with BuildPart() as block:
        Box(40, 20, 10)
        Cylinder(hole, 20, mode=Mode.SUBTRACT)
        if fillets:
            fillet(block.edges().filter_by(Axis.Z), radius=3)
    assert block.part is not None
    return block.part


def _part(tmp_path: Path, name: str, hole: float = 2, *, fillets: bool = False):
    with tracing() as trace:
        with BuildPart() as block:
            Box(40, 20, 10)
            Cylinder(hole, 20, mode=Mode.SUBTRACT)
            if fillets:
                fillet(block.edges().filter_by(Axis.Z), radius=3)
    assert block.part is not None
    part = block.part
    part.label = name
    data, leaves = export_trace(trace, part, Provenance(trace).face_origins(part), root=tmp_path, build_seconds=0)
    shapes = tmp_path / f"{name}-{hole}-{fillets}.shapes"
    save(leaves, shapes)
    return data, part, Build(json.dumps(data).encode(), False, shapes, {})


def _setup(tmp_path: Path, *, folder: bool = False, fillets: bool = False):
    builds = _Builds()
    first, _, first_build = _part(tmp_path, "A", fillets=fillets)
    assert first_build.shapes is not None
    builds.put({"kind": "part", "name": "A"}, first_build)
    project = Project("model:build", (), ("A", "B"))
    state: dict[str, Any] = {
        "target": {"kind": "part", "name": "A"},
        "status": "ok",
        "shapes": first_build.shapes.name,
        "partShapes": {},
        "selection": [],
        "hidden": [],
    }
    if folder:
        second, _, second_build = _part(tmp_path, "B", hole=3)
        builds.put({"kind": "part", "name": "B"}, second_build)
        assert second_build.shapes is not None
        scene = tessellate(
            [
                SceneObject("A", _independent_part(2, fillets=fillets)),
                SceneObject("B", _independent_part(3)),
            ]
        )
        body = {"scene": {"data": scene.data}, "build_seconds": 0}
        shapes = tmp_path / "folder.shapes"
        save(scene.leaves, shapes)
        target = {"kind": "file", "path": "Parts"}
        builds.put(target, Build(json.dumps(body).encode(), False, shapes, {}))
        state.update(
            target=target,
            shapes=shapes.name,
            partShapes={"A": first_build.shapes.name, "B": second_build.shapes.name},
        )
        assert second["scene"]["part"]
    snapshot = Snapshot(cast(Builds, builds), Measurer(tmp_path), project, tmp_path)
    return snapshot, builds, state, first


def _face(trace: dict[str, Any], kind: str) -> int:
    return next(op["faces"][0] for op in trace["ops"] if op["kind"] == kind and op["faces"])


def _folder_path(builds: _Builds, state: dict[str, Any], part: str) -> str:
    build = builds.latest(state["target"])
    assert build is not None and build.body is not None
    body = json.loads(build.body)
    return next(node["id"] for node in context.leaves(body["scene"]["data"]["shapes"]) if node["name"] == part)


def test_a_copied_face_is_standalone_with_captured_maker_source_and_geometry(tmp_path: Path):
    snapshot, builds, state, trace = _setup(tmp_path)
    path = f"{trace['scene']['part']}/faces/faces_{_face(trace, 'Cylinder')}"
    state["selection"] = [{"kind": "entity", "path": path}]
    build = builds.latest(state["target"])
    assert build is not None and build.body is not None and build.shapes is not None
    # The built trace's source, not a later read of the model file, is authoritative.
    captured = json.loads(build.body)
    captured["sources"] = {
        file: source.replace("Cylinder(hole, 20", "Cylinder(CAPTURED_HOLE, 20")
        for file, source in captured["sources"].items()
    }
    body = json.dumps(captured).encode()
    builds.put(state["target"], Build(body, False, build.shapes, {}))

    copied = snapshot.create("tab-a", state)
    text = copied["text"]
    assert text.startswith("# Cadiste selection v1\n")
    assert set(copied) == {"text", "label"}
    assert "cadiste:ref/" not in text
    assert "Captured at:" in text and f"Project root: {tmp_path}" in text
    assert "millimetres (mm)" in text and "degrees" in text
    assert "captured, not-live" in text and "Viewer tab: tab-a" in text
    assert hashlib.sha256(body).hexdigest() in text and build.shapes.name in text
    assert path in text and "cylinder" in text and "Diameter" in text
    assert "Creating feature detail" in text and "Cylinder subtract" in text
    assert "radius =" in text and "CAPTURED_HOLE" in text
    # The copied payload remains complete after all server-side build data has gone.
    builds.entries.clear()
    build.shapes.unlink()
    assert "CAPTURED_HOLE" in copied["text"] and path in copied["text"]


def test_mixed_folder_selection_keeps_all_items_and_pair_measurements(tmp_path: Path):
    snapshot, builds, state, trace = _setup(tmp_path, folder=True)
    first = _folder_path(builds, state, "A")
    second = _folder_path(builds, state, "B")
    face = _face(trace, "Box")
    items = [
        {"kind": "feature", "part": "A", "node": "o0"},
        {"kind": "feature", "part": "B", "node": "o1"},
        {"kind": "instance", "path": second},
        {"kind": "entity", "path": f"{first}/faces/faces_{face}"},
        {"kind": "entity", "path": f"{second}/faces/faces_{face}"},
    ]
    state["selection"] = [*items, items[0]]

    text = snapshot.create(None, state)["text"]

    assert "5 item(s), in selection order" in text
    assert "### 1. Feature o0 of part 'A'" in text
    assert "### 2. Feature o1 of part 'B'" in text
    assert f"### 3. Instance {second}" in text
    assert text.index("### 1.") < text.index("### 2.") < text.index("### 3.") < text.index("### 4.")
    assert "### 5. Entity" in text and "### 6." not in text
    assert "owning part: B" in text
    assert "Minimum distance" in text
    assert "Provenance mapping: picked region and adjacent face indices agree geometrically" in text
    assert f"Canonical part path: {trace['scene']['part']}" in text
    assert "Creating feature detail (captured source and arguments):" in text
    assert "Box add" in text and "Cylinder subtract" in text


def test_empty_composite_selection_enumerates_every_member_and_known_part(tmp_path: Path):
    snapshot, builds, state, _ = _setup(tmp_path, folder=True)

    text = snapshot.create("tab-folder", state)["text"]

    assert "Entire current target" in text and "Members (2)" in text
    for part in ("A", "B"):
        assert f"owning part: {part}" in text
        assert _folder_path(builds, state, part) in text


def test_edges_and_vertices_include_full_adjacent_maker_context(tmp_path: Path):
    snapshot, _, state, trace = _setup(tmp_path, fillets=True)
    shapes = load(tmp_path / state["shapes"])
    part = trace["scene"]["part"]
    wanted = {next(op["index"] for op in trace["ops"] if op["kind"] == kind) for kind in ("Box", "fillet")}
    edge = next(
        f"{part}/edges/edges_{index}"
        for index in range(len(shapes.leaves[part]["edges"] or []))
        if {
            maker["index"]
            for face in adjacent_faces(shapes, f"{part}/edges/edges_{index}") or []
            if (maker := context.face_maker(trace, face)) is not None
        }
        == wanted
    )
    state["selection"] = [
        {"kind": "entity", "path": edge},
        {"kind": "entity", "path": f"{part}/vertices/vertices_0"},
    ]

    text = snapshot.create(None, state)["text"]

    assert "Adjacent/result faces:" in text
    assert "Box add" in text and "fillet replace" in text
    assert "radius =" in text and "fillet(block.edges().filter_by(Axis.Z), radius=3)" in text
    assert "source:" in text and "### 2. Entity" in text


def test_three_picked_entities_preserve_geometry_and_totals_without_expanding_all_pairs(tmp_path: Path):
    snapshot, _, state, trace = _setup(tmp_path)
    part = trace["scene"]["part"]
    state["selection"] = [{"kind": "entity", "path": f"{part}/faces/faces_{n}"} for n in range(3)]

    text = snapshot.create(None, state)["text"]

    assert "3 item(s)" in text and text.count("Geometry:") == 3
    assert "Total area" in text and "Minimum distance" not in text


def test_a_rebuilt_target_is_rejected_instead_of_using_newer_face_numbers(tmp_path: Path):
    snapshot, builds, state, _ = _setup(tmp_path)
    _, _, newer = _part(tmp_path, "A", hole=4)
    builds.put(state["target"], newer)

    with pytest.raises(SnapshotError, match=r"Reload/rebuild.*no newer build was substituted"):
        snapshot.create(None, state)


def test_missing_identity_is_not_replaced_by_the_latest_build(tmp_path: Path):
    snapshot, _, state, _ = _setup(tmp_path)
    state.pop("shapes")

    with pytest.raises(SnapshotError, match="unavailable or expired"):
        snapshot.create(None, state)


def test_missing_or_mismatched_folder_trace_reports_why_provenance_is_unavailable(tmp_path: Path):
    snapshot, builds, state, trace = _setup(tmp_path, folder=True)
    leaf = _folder_path(builds, state, "A")
    path = f"{leaf}/faces/faces_{_face(trace, 'Cylinder')}"
    state["selection"] = [{"kind": "entity", "path": path}]
    state["partShapes"] = {}
    text = snapshot.create(None, state)["text"]
    assert "Geometry: cylinder" in text
    assert "Creating feature/source unavailable: no pinned owning-part trace was loaded" in text
    _, _, different = _part(tmp_path, "A", hole=4)
    builds.put({"kind": "part", "name": "A"}, different)
    assert different.shapes is not None
    state["partShapes"] = {"A": different.shapes.name}
    text = snapshot.create(None, state)["text"]
    assert "do not geometrically match the pinned part trace" in text
    assert "Creating feature detail" not in text


def test_unrelated_expired_traces_do_not_block_selected_part(tmp_path: Path):
    snapshot, _, state, _ = _setup(tmp_path, folder=True)
    state["selection"] = [{"kind": "feature", "part": "A", "node": "o0"}]
    state["partShapes"]["B"] = "expired.shapes"

    text = snapshot.create(None, state)["text"]

    assert "Box add" in text and "expired.shapes" not in text


def test_expired_selected_part_trace_is_never_silently_substituted(tmp_path: Path):
    snapshot, _, state, _ = _setup(tmp_path, folder=True)
    state["selection"] = [{"kind": "feature", "part": "A", "node": "o0"}]
    state["partShapes"]["A"] = "expired.shapes"

    text = snapshot.create(None, state)["text"]

    assert "Feature provenance unavailable" in text and "Reload/rebuild" in text
    assert "Box add" not in text


def test_one_invalid_entity_does_not_drop_other_selected_geometry(tmp_path: Path):
    snapshot, _, state, trace = _setup(tmp_path)
    good = f"{trace['scene']['part']}/faces/faces_{_face(trace, 'Cylinder')}"
    invalid = "/missing/faces/faces_999"
    state["selection"] = [{"kind": "entity", "path": good}, {"kind": "entity", "path": invalid}]

    text = snapshot.create(None, state)["text"]

    assert good in text and invalid in text
    assert "Geometry: cylinder" in text and "Geometry: unavailable" in text
    assert "Relationships unavailable" in text


def test_unknown_feature_is_preserved_with_an_explicit_unavailable_detail(tmp_path: Path):
    snapshot, _, state, _ = _setup(tmp_path)
    state["selection"] = [{"kind": "feature", "part": "A", "node": "o999"}]

    text = snapshot.create(None, state)["text"]

    assert "Feature o999 of part 'A'" in text and "Feature detail unavailable" in text


def test_selection_validation_and_agent_live_command_use_the_canonical_items(tmp_path: Path):
    snapshot, builds, state, _ = _setup(tmp_path)
    items = cast(list[SelectionItem], [{"kind": "feature", "part": "A", "node": "o0"}])
    calls = []

    class _Hub:
        def linked_tab(self, session: str, timeout: float):
            return "tab-a"

        def tab_state(self, tab: str):
            return state

        def call(self, tab: str, command: str, params: dict[str, Any], timeout: float):
            calls.append((tab, command, params))
            state["selection"] = params["items"]

    agent = Agent(cast(Hub, _Hub()), cast(Builds, builds), snapshot.measurer, snapshot.project, tmp_path)
    answer = agent.select("session", [*items, *items])
    assert calls == [("tab-a", "select", {"items": items})]
    assert "1 item(s)" in answer.text
    assert "get_reference" not in agent.tools
    assert "Entire current target" in agent.select("session", []).text
    assert selection_items([*items, *items]) == items
    with pytest.raises(SnapshotError, match="invalid selection item"):
        selection_items([{"kind": "feature", "node": "o0"}])


def test_legacy_selection_fields_are_not_silently_treated_as_an_empty_selection(tmp_path: Path):
    snapshot, _, state, _ = _setup(tmp_path)
    state.pop("selection")
    state["selected"] = "o0"

    with pytest.raises(SnapshotError, match="selection must be a list"):
        snapshot.create(None, state)


@pytest.mark.parametrize("topology", ["faces", "edges", "vertices"])
def test_independently_built_and_serialized_folder_entities_keep_maker_source(tmp_path: Path, topology: str):
    snapshot, builds, state, trace = _setup(tmp_path, folder=True, fillets=True)
    leaf = _folder_path(builds, state, "A")
    index = _face(trace, "Cylinder") if topology == "faces" else 0
    state["selection"] = [{"kind": "entity", "path": f"{leaf}/{topology}/{topology}_{index}"}]

    text = snapshot.create(None, state)["text"]

    assert "Creating feature detail (captured source and arguments):" in text
    assert "0.000001 mm OCC tolerance" in text
    assert "source:" in text and "arguments (parameter = expression written" in text
    assert "Creating feature/source unavailable" not in text


def test_recorded_instance_placement_maps_geometry_to_trace_and_is_captured_in_text(tmp_path: Path):
    snapshot, builds, state, trace = _setup(tmp_path, folder=True)
    part = _independent_part(2).moved(Location((30, 12, 4), (0, 0, 35)))
    scene = tessellate([SceneObject("A", part)])
    shapes = tmp_path / "placed.shapes"
    save(scene.leaves, shapes)
    body = {"scene": {"data": scene.data}, "build_seconds": 0}
    builds.put(state["target"], Build(json.dumps(body).encode(), False, shapes, {}))
    state["shapes"] = shapes.name
    leaf = scene.paths["A"]
    state["selection"] = [
        {"kind": "entity", "path": f"{leaf}/faces/faces_{_face(trace, 'Cylinder')}"},
        {"kind": "instance", "path": leaf},
    ]

    text = snapshot.create(None, state)["text"]

    assert "Creating feature detail" in text and "Cylinder subtract" in text
    assert "Creating feature/source unavailable" not in text
    assert "Recorded scene-chain placement" in text
    assert "local translation [30" in text and "quaternion component order is x, y, z, w" in text
    assert "#### Owning part 'A': captured trace context" in text
    assert "Captured feature tree:" in text and "Captured source entry point(s):" in text


def test_selected_groups_and_instances_deduplicate_captured_owning_part_source(tmp_path: Path):
    snapshot, builds, state, _ = _setup(tmp_path, folder=True)
    build = builds.latest(state["target"])
    assert build is not None and build.body is not None
    root = json.loads(build.body)["scene"]["data"]["shapes"]["id"]
    state["selection"] = [
        {"kind": "instance", "path": _folder_path(builds, state, "A")},
        {"kind": "instance", "path": root},
        {"kind": "instance", "path": _folder_path(builds, state, "B")},
    ]

    text = snapshot.create(None, state)["text"]

    assert text.count("#### Owning part 'A': captured trace context") == 1
    assert text.count("#### Owning part 'B': captured trace context") == 1
    assert text.count("Captured source entry point(s):") == 2
    assert "shared owner" in text and "Cylinder subtract" in text and "source:" in text
    assert "Recorded scene-chain placement" in text


def test_permuted_face_indices_cannot_borrow_another_regions_maker(tmp_path: Path):
    _, _, state, trace = _setup(tmp_path)
    shapes = load(tmp_path / state["shapes"])
    leaf = trace["scene"]["part"]
    faces = shapes.leaves[leaf]["faces"]
    assert faces is not None
    permuted = [faces[1], faces[0], *faces[2:]]
    other = Shapes({leaf: {**shapes.leaves[leaf], "faces": permuted}})
    path = f"{leaf}/faces/faces_0"

    assert not corresponding_entity(shapes, path, other, path, [], [])


@pytest.mark.parametrize("topology", ["edges", "vertices"])
def test_permuted_adjacent_faces_cannot_change_edge_or_vertex_maker_context(tmp_path: Path, topology: str):
    _, _, state, trace = _setup(tmp_path)
    shapes = load(tmp_path / state["shapes"])
    leaf = trace["scene"]["part"]
    path = f"{leaf}/{topology}/{topology}_0"
    faces = shapes.leaves[leaf]["faces"]
    adjacent = adjacent_faces(shapes, path)
    assert faces is not None and adjacent
    first = adjacent[0]
    second = next(index for index in range(len(faces)) if index not in adjacent)
    permuted = list(faces)
    permuted[first], permuted[second] = permuted[second], permuted[first]
    other = Shapes({leaf: {**shapes.leaves[leaf], "faces": permuted}})

    assert not corresponding_entity(shapes, path, other, path, [], [])


def test_equal_area_bounds_and_support_surface_do_not_establish_trimmed_face_correspondence():
    regions = []
    for offset in (-5, 5):
        with BuildPart() as block:
            Box(40, 20, 10)
            with Locations((offset, 0, 0)):
                Cylinder(2, 20, mode=Mode.SUBTRACT)
        assert block.part is not None
        cap = block.part.faces().sort_by(Axis.Z)[-1]
        assert cap.wrapped is not None
        regions.append(Shapes({"part": {"faces": [cap.wrapped], "edges": [], "vertices": []}}))
    path = "part/faces/faces_0"

    assert not corresponding_entity(regions[0], path, regions[1], path, [], [])
