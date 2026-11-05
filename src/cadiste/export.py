"""A trace as JSON for the viewer: the feature tree, every operation and builder, face origins, the source of the
model files the trace points into, and the scene to render (the part, its sketches and their elements)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from build123d import Mode
from build123d.topology import Shape

from .mesh import Leaf, SceneObject, tessellate
from .provenance import OriginPath
from .source import capture_sources, source_revision
from .trace import BuilderRecord, CallSite, Op, Trace
from .tree import BuilderNode, Group, Node, OpNode, feature_tree

_MODES = {
    Mode.ADD: "add",
    Mode.SUBTRACT: "subtract",
    Mode.INTERSECT: "intersect",
    Mode.REPLACE: "replace",
    Mode.PRIVATE: "private",
}

_SKETCH_COLOR = "#4aa3ff"
_ELEMENT_COLOR = "#ff9f43"


def _has_geometry(shape: Shape | None) -> bool:
    return shape is not None and bool(shape.faces() or shape.edges())


def part_scene(trace: Trace, result: Shape) -> tuple[dict[str, Any], list[Leaf]]:
    """The part, every sketch where it was drawn, and every sketch element in place, in one scene: the viewer
    shows or hides them as the selection changes."""
    records = {id(record.builder): record for record in trace.builders if record.builder is not None}
    part_name = result.label or type(result).__name__
    objects = [SceneObject(part_name, result)]
    sketches: dict[int, tuple[str, list[float] | None]] = {}
    elements: dict[int, str] = {}
    for record in trace.builders:
        if record.kind == "BuildSketch" and _has_geometry(record.product):
            assert record.product is not None
            name = f"sketch {record.index} {record.name}".rstrip()
            faces = record.product.faces()
            normal = faces[0].normal_at() if faces else None
            sketches[record.index] = (name, None if normal is None else [normal.X, normal.Y, normal.Z])
            objects.append(SceneObject(name, record.product, _SKETCH_COLOR, 0.7))
        if record.kind in ("BuildSketch", "BuildLine"):
            for op in record.ops:
                placed = record.placed(op.obj, records) if op.obj is not None else None
                if _has_geometry(placed):
                    assert placed is not None
                    elements[op.index] = f"op {op.index} {op.kind}"
                    objects.append(SceneObject(elements[op.index], placed, _ELEMENT_COLOR, 1.0))
    scene = tessellate(objects)
    return {
        "data": scene.data,
        "part": scene.paths[part_name],
        # Face highlighting and picking need the viewer's face numbers to be the part's.
        "face_ids": scene.faces.get(part_name) == len(result.faces()),
        "sketches": {
            index: {"path": scene.paths[name], "normal": normal}
            for index, (name, normal) in sketches.items()
            if name in scene.paths
        },
        "elements": {index: scene.paths[name] for index, name in elements.items() if name in scene.paths},
    }, scene.leaves


def assembly_scene(assembly: Shape) -> tuple[dict[str, Any], list[Leaf]]:
    scene = tessellate([SceneObject(assembly.label or type(assembly).__name__, assembly)])
    # A lone assembly becomes the scene's root group rather than a child of it.
    return {"data": scene.data, "root": scene.data["shapes"]["id"]}, scene.leaves


class _Exporter:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.files: set[str] = set()
        self.groups = 0
        self.invocations: dict[str, dict[str, Any]] = {}

    def path(self, file: str) -> str:
        """Model files relative to the project root when they are under it."""
        resolved = Path(file).resolve()
        shown = str(resolved.relative_to(self.root)) if resolved.is_relative_to(self.root) else str(resolved)
        self.files.add(file)
        return shown

    def site(self, site: CallSite) -> dict[str, Any]:
        file = self.path(site.file)
        self.invocations.setdefault(
            str(site.invocation),
            {
                "file": file,
                "function": site.function,
                "label": site.label,
                "parameter_capture": "first traced event",
                "parameters": list(site.parameters),
            },
        )
        return {
            "invocation": site.invocation,
            "file": file,
            "line": site.line,
            "function": site.function,
            "label": site.label,
            "code": site.code,
        }

    def node(self, node: Node) -> dict[str, Any]:
        match node:
            case OpNode(op=op):
                return {"id": f"o{op.index}", "type": "op", "op": op.index}
            case BuilderNode(builder=record, children=children):
                return {
                    "id": f"b{record.index}",
                    "type": "builder",
                    "builder": record.index,
                    "children": [self.node(child) for child in children],
                }
            case Group(site=site, called_at=called_at, children=children):
                self.groups += 1
                return {
                    "id": f"g{self.groups}",
                    "type": "group",
                    "invocation": site.invocation,
                    "label": site.label,
                    "function": site.function,
                    "defined_in": self.path(site.file),
                    "called_at": self.site(called_at),
                    "children": [self.node(child) for child in children],
                }

    def op(self, op: Op, made: list[int], via: list[int]) -> dict[str, Any]:
        return {
            "index": op.index,
            "kind": op.kind,
            "mode": _MODES[op.mode],
            "builder": op.builder.index,
            "site": [self.site(site) for site in op.site],
            "inputs": [record.index for record in op.inputs],
            "child": op.child.index if op.child is not None else None,
            "arguments": [{"name": a.name, "value": a.value, "source": a.source} for a in op.arguments],
            "faces": made,
            "via_faces": via,
        }

    def builder(self, record: BuilderRecord, consumed_by: list[int]) -> dict[str, Any]:
        published = next((op.index for op in record.parent.ops if op.child is record), None) if record.parent else None
        return {
            "index": record.index,
            "kind": record.kind,
            "mode": _MODES[record.mode],
            "name": record.name,
            "parent": record.parent.index if record.parent is not None else None,
            "site": [self.site(site) for site in record.site],
            "owner": type(record.owner).__name__ if record.owner is not None else None,
            "ops": [op.index for op in record.ops],
            "published_by": published,
            "consumed_by": consumed_by,
        }


def _size(shape: Shape) -> dict[str, Any]:
    """Volume (mm³) and axis-aligned bounds (mm): what an edit's effect on the part is first judged by."""
    box = shape.bounding_box()
    volume = sum(solid.volume for solid in shape.solids())
    return {"volume": volume, "bbox": {"min": list(box.min), "max": list(box.max)}}


def export_trace(
    trace: Trace,
    result: Shape,
    origins: list[OriginPath | None],
    *,
    root: Path,
    build_seconds: float,
) -> tuple[dict[str, Any], list[Leaf]]:
    """The viewer's JSON for one traced build of ``result``, and its scene's leaves (to measure them);
    ``origins`` are its faces' (``Provenance``)."""
    exporter = _Exporter(root)
    made: dict[int, list[int]] = {op.index: [] for op in trace.ops}
    via: dict[int, list[int]] = {op.index: [] for op in trace.ops}
    for face, path in enumerate(origins):
        if path is None:
            continue
        made[path[-1].index].append(face)
        for op in path[:-1]:
            via[op.index].append(face)
    consumed: dict[int, list[int]] = {record.index: [] for record in trace.builders}
    for op in trace.ops:
        for record in op.inputs:
            consumed[record.index].append(op.index)

    scene, leaves = part_scene(trace, result)
    roots = [
        exporter.node(feature_tree(record)) for record in trace.builders if record.parent is None and record.events
    ]
    data = {
        "result": {
            "type": type(result).__name__,
            "faces": len(origins),
            "attributed": sum(path is not None for path in origins),
            **_size(result),
        },
        "build_seconds": build_seconds,
        "roots": roots,
        "ops": [exporter.op(op, made[op.index], via[op.index]) for op in trace.ops],
        "builders": [exporter.builder(record, consumed[record.index]) for record in trace.builders],
        "scene": scene,
    }
    data["sources"] = capture_sources(exporter.files, root)
    data["source_revision"] = source_revision(data["sources"])
    data["invocations"] = exporter.invocations
    return data, leaves
