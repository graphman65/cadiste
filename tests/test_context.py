from __future__ import annotations

from pathlib import Path
from typing import Any

from build123d import Axis, Box, BuildPart, Cylinder, Mode, Part, fillet

from cadiste import Provenance, tracing
from cadiste.context import compare, part_selection
from cadiste.export import export_trace
from cadiste.measure import adjacent_faces, load, save


def _block(hole: float, *, fillets: bool) -> Part:
    with BuildPart() as block:
        Box(40, 20, 10)
        Cylinder(hole, 20, mode=Mode.SUBTRACT)
        if fillets:
            fillet(block.edges().filter_by(Axis.Z), radius=3)
    assert block.part is not None
    return block.part


def _exported(tmp_path: Path, hole: float, *, fillets: bool) -> tuple[dict[str, Any], Path]:
    """The viewer's JSON of a traced build and the file of its shapes, as `cadiste export --shapes` writes them."""
    with tracing() as trace:
        part = _block(hole, fillets=fillets)
    data, leaves = export_trace(trace, part, Provenance(trace).face_origins(part), root=tmp_path, build_seconds=0)
    shapes = tmp_path / f"block-{hole}-{fillets}.shapes"
    save(leaves, shapes)
    return data, shapes


def test_a_changed_argument_and_an_added_operation_are_reported_with_the_volume_change(tmp_path: Path):
    before, _ = _exported(tmp_path, 2, fillets=False)
    after, _ = _exported(tmp_path, 3, fillets=True)

    report = compare(before, after)

    assert "operations: 2 → 3" in report
    (changed,) = [line for line in report.splitlines() if line.lstrip().startswith("~")]
    assert "Cylinder subtract" in changed
    assert "radius: value 2 → 3" in report
    assert any(line.lstrip().startswith("+") and "fillet replace" in line for line in report.splitlines())
    assert "volume: 7874.34 → " in report  # 40 x 20 x 10 less a 4 mm hole through it
    assert "unchanged" in report.split("bounding box:")[1].splitlines()[0]


def test_an_unchanged_rebuild_says_so():
    data = {"result": {"faces": 1, "attributed": 1}, "ops": [], "builders": []}

    assert "the same 0, called with the same arguments" in compare(data, data)


def test_a_picked_edge_names_the_operations_of_the_faces_it_bounds(tmp_path: Path):
    trace, file = _exported(tmp_path, 2, fillets=True)
    shapes = load(file)
    part = trace["scene"]["part"]
    fillet_op = next(op for op in trace["ops"] if op["kind"] == "fillet")
    box_op = next(op for op in trace["ops"] if op["kind"] == "Box")
    # An edge where a fillet face meets a face of the box.
    wanted = {fillet_op["index"], box_op["index"]}
    edges = [f"{part}/edges/edges_{index}" for index in range(len(shapes.leaves[part]["edges"] or []))]
    edge = next(path for path in edges if {_maker(trace, f) for f in adjacent_faces(shapes, path) or []} == wanted)

    text = part_selection(trace, None, [edge], {}, [], lambda path: adjacent_faces(shapes, path))

    assert "bounds 2 face(s)" in text
    assert f"[o{fillet_op['index']}] #{fillet_op['index']} fillet replace" in text
    assert f"[o{box_op['index']}] #{box_op['index']} Box add" in text


def _maker(trace: dict[str, Any], face: int) -> int | None:
    return next((op["index"] for op in trace["ops"] if face in op["faces"]), None)
