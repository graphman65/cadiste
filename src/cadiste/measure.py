"""Measurements of what the viewer shows, exact: taken on the B-rep its mesh was made from, not on the mesh.

A build writes its scene's faces, edges and vertices next to its JSON (``save``), placed and numbered the way the
viewer has them; the server reads them back (``load``) and answers for a selection of the viewer's paths
(``measure``): the properties of one entity (a cylinder's diameter, an edge's length), the distances and the angle
between two, the total length or area of several.
"""

from __future__ import annotations

import io
import json
import math
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# OCP is a compiled extension without type stubs: pyright cannot see its symbols.
# pyright: reportAttributeAccessIssue=false
from OCP.BinTools import BinTools, BinTools_FormatVersion
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.gp import gp_Ax1
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Iterator, TopoDS_Shape

from .mesh import Leaf

_INDEX = "index.json"
_SHAPES = "shapes.bin"
_TOPOS = {"faces": "face", "edges": "edge", "vertices": "vertex"}
"""The viewer's list names, with the topology of their items."""
_PATH = re.compile(r"(?P<leaf>.+)/(?P<topo>faces|edges|vertices)/(?P=topo)_(?P<index>\d+)")
_PARALLEL = 1e-9
"""Angle (rad) under which two directions count as parallel."""
_SAME = 1e-6
"""Distance (mm) under which a derived distance repeats the minimum distance."""

Vec = tuple[float, float, float]


class MeasureError(Exception):
    """A selection that cannot be measured; the message says why."""


def save(leaves: Sequence[Leaf], file: Path) -> None:
    """The leaves' faces, edges and vertices, in a zip: an index of the leaves and one binary B-rep (without
    triangulations) of a compound holding, per leaf, a compound of each list."""
    builder = BRep_Builder()
    root = TopoDS_Compound()
    builder.MakeCompound(root)
    index = []
    for leaf in leaves:
        lists = (leaf.faces, leaf.edges, leaf.vertices)
        for items in lists:
            part = TopoDS_Compound()
            builder.MakeCompound(part)
            for item in items or ():
                builder.Add(part, item)
            builder.Add(root, part)
        present = {topo: items is not None for topo, items in zip(_TOPOS, lists, strict=True)}
        index.append({"path": leaf.path, **present})
    stream = io.BytesIO()
    BinTools.Write_s(root, stream, False, False, BinTools_FormatVersion.BinTools_FormatVersion_CURRENT)
    with zipfile.ZipFile(file, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_INDEX, json.dumps(index))
        archive.writestr(_SHAPES, stream.getvalue())


def _children(shape: TopoDS_Shape) -> list[TopoDS_Shape]:
    children = []
    iterator = TopoDS_Iterator(shape)
    while iterator.More():
        children.append(iterator.Value())
        iterator.Next()
    return children


@dataclass(frozen=True)
class Shapes:
    """A scene's entities by leaf path, then by kind (``faces``...): None where the viewer's numbers are not the
    shape's."""

    leaves: dict[str, dict[str, list[TopoDS_Shape] | None]]

    def entity(self, path: str) -> tuple[str, TopoDS_Shape]:
        """The topology (``face``, ``edge``, ``vertex``) and shape of a viewer component path."""
        match = _PATH.fullmatch(path)
        if match is None:
            raise MeasureError(f"{path} is not a face, an edge or a vertex")
        leaf = self.leaves.get(match["leaf"])
        if leaf is None:
            raise MeasureError(f"{match['leaf']} is not in this build")
        items = leaf[match["topo"]]
        if items is None:
            raise MeasureError(f"the {match['topo']} of {match['leaf']} are not numbered as the mesh's")
        index = int(match["index"])
        if index >= len(items):
            raise MeasureError(f"{path} is not in this build")
        return _TOPOS[match["topo"]], items[index]


def load(file: Path) -> Shapes:
    with zipfile.ZipFile(file) as archive:
        index = json.loads(archive.read(_INDEX))
        data = archive.read(_SHAPES)
    root = TopoDS_Shape()
    BinTools.Read_s(root, io.BytesIO(data))
    lists = _children(root)
    leaves: dict[str, dict[str, list[TopoDS_Shape] | None]] = {}
    for at, entry in enumerate(index):
        leaves[entry["path"]] = {
            topo: [_downcast(item) for item in _children(lists[3 * at + k])] if entry[topo] else None
            for k, topo in enumerate(_TOPOS)
        }
    return Shapes(leaves)


def _downcast(shape: TopoDS_Shape) -> TopoDS_Shape:
    kind = shape.ShapeType()
    if kind == TopAbs_FACE:
        return TopoDS.Face(shape)
    if kind == TopAbs_EDGE:
        return TopoDS.Edge(shape)
    if kind == TopAbs_VERTEX:
        return TopoDS.Vertex(shape)
    return shape


# Vectors as plain tuples: the few operations measuring needs.


def _vec(p: Any) -> Vec:
    return (p.X(), p.Y(), p.Z())


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a: Vec, s: float) -> Vec:
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a: Vec) -> float:
    return math.sqrt(_dot(a, a))


def _angle(a: Vec, b: Vec) -> float:
    """Angle between two unit directions as lines (sign ignored), in [0, pi/2]."""
    return math.acos(min(1.0, abs(_dot(a, b))))


@dataclass(frozen=True)
class _Line:
    point: Vec
    direction: Vec
    """Unit."""

    @staticmethod
    def of(axis: gp_Ax1) -> _Line:
        return _Line(_vec(axis.Location()), _vec(axis.Direction()))

    def foot(self, p: Vec) -> Vec:
        """Projection of ``p`` on the line."""
        return _add(self.point, _scale(self.direction, _dot(_sub(p, self.point), self.direction)))


def _closest(a: _Line, b: _Line, near: Vec) -> tuple[Vec, Vec]:
    """Closest points of two lines (for parallel lines, the pair through the foot of ``near`` on ``a``)."""
    w = _sub(a.point, b.point)
    d = _dot(a.direction, b.direction)
    denominator = 1 - d * d
    if denominator < _PARALLEL:
        on_a = a.foot(near)
        return on_a, b.foot(on_a)
    s = (d * _dot(b.direction, w) - _dot(a.direction, w)) / denominator
    t = (_dot(b.direction, w) - d * _dot(a.direction, w)) / denominator
    return _add(a.point, _scale(a.direction, s)), _add(b.point, _scale(b.direction, t))


@dataclass
class _Entity:
    path: str
    topo: str
    shape: TopoDS_Shape
    kind: str
    """Its geometry: ``plane``, ``cylinder``, ``line``, ``circle``, ``arc``, ``point``..."""
    properties: list[dict[str, Any]] = field(default_factory=list)
    center: Vec | None = None
    """A vertex's position; the centre of a circle, an ellipse or a sphere."""
    axis: _Line | None = None
    """The axis of a cylinder, a cone or a torus."""
    line: _Line | None = None
    """The line a straight edge lies on."""
    plane: tuple[Vec, Vec] | None = None
    """A planar face's plane: a point and the unit normal."""
    length: float = 0.0
    area: float = 0.0

    @property
    def direction(self) -> tuple[str, Vec] | None:
        """What an angle is measured on: a line's direction or a plane's normal."""
        if self.plane is not None:
            return "plane", self.plane[1]
        line = self.line or self.axis
        return None if line is None else ("line", line.direction)


def _quantity(label: str, kind: str, value: float | Vec, line: tuple[Vec, Vec] | None = None) -> dict[str, Any]:
    """A row of the answer: ``kind`` is ``length``, ``area``, ``angle`` (degrees), ``point`` or ``direction``; a
    distance carries the segment it spans, for the viewer to draw."""
    quantity: dict[str, Any] = {"label": label, "kind": kind, "value": value}
    if line is not None:
        quantity["line"] = line
    return quantity


_SURFACES = {
    GeomAbs_SurfaceType.GeomAbs_Plane: "plane",
    GeomAbs_SurfaceType.GeomAbs_Cylinder: "cylinder",
    GeomAbs_SurfaceType.GeomAbs_Cone: "cone",
    GeomAbs_SurfaceType.GeomAbs_Sphere: "sphere",
    GeomAbs_SurfaceType.GeomAbs_Torus: "torus",
    GeomAbs_SurfaceType.GeomAbs_BezierSurface: "Bézier",
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface: "B-spline",
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution: "revolved",
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion: "extruded",
    GeomAbs_SurfaceType.GeomAbs_OffsetSurface: "offset",
}
_CURVES = {
    GeomAbs_CurveType.GeomAbs_Line: "line",
    GeomAbs_CurveType.GeomAbs_Circle: "circle",
    GeomAbs_CurveType.GeomAbs_Ellipse: "ellipse",
    GeomAbs_CurveType.GeomAbs_Hyperbola: "hyperbola",
    GeomAbs_CurveType.GeomAbs_Parabola: "parabola",
    GeomAbs_CurveType.GeomAbs_BezierCurve: "Bézier",
    GeomAbs_CurveType.GeomAbs_BSplineCurve: "B-spline",
    GeomAbs_CurveType.GeomAbs_OffsetCurve: "offset",
}


def _face(path: str, face: Any) -> _Entity:
    surface = BRepAdaptor_Surface(face)
    kind = surface.GetType()
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    entity = _Entity(path, "face", face, _SURFACES.get(kind, "other"), area=props.Mass())
    area = _quantity("Area", "area", entity.area)
    if kind == GeomAbs_SurfaceType.GeomAbs_Plane:
        position = surface.Plane().Position()
        entity.plane = (_vec(position.Location()), _vec(position.Direction()))
        entity.properties = [area, _quantity("Normal", "direction", entity.plane[1])]
    elif kind == GeomAbs_SurfaceType.GeomAbs_Cylinder:
        cylinder = surface.Cylinder()
        entity.axis = _Line.of(cylinder.Axis())
        r = cylinder.Radius()
        entity.properties = [
            _quantity("Diameter", "length", 2 * r),
            _quantity("Radius", "length", r),
            area,
            _quantity("Axis", "direction", entity.axis.direction),
        ]
    elif kind == GeomAbs_SurfaceType.GeomAbs_Cone:
        cone = surface.Cone()
        entity.axis = _Line.of(cone.Axis())
        entity.properties = [
            _quantity("Half-angle", "angle", math.degrees(abs(cone.SemiAngle()))),
            area,
            _quantity("Axis", "direction", entity.axis.direction),
        ]
    elif kind == GeomAbs_SurfaceType.GeomAbs_Sphere:
        sphere = surface.Sphere()
        entity.center = _vec(sphere.Location())
        r = sphere.Radius()
        entity.properties = [
            _quantity("Diameter", "length", 2 * r),
            _quantity("Radius", "length", r),
            _quantity("Center", "point", entity.center),
            area,
        ]
    elif kind == GeomAbs_SurfaceType.GeomAbs_Torus:
        torus = surface.Torus()
        entity.axis = _Line.of(torus.Axis())
        entity.properties = [
            _quantity("Major radius", "length", torus.MajorRadius()),
            _quantity("Minor radius", "length", torus.MinorRadius()),
            area,
            _quantity("Axis", "direction", entity.axis.direction),
        ]
    else:
        entity.properties = [area]
    return entity


def _edge(path: str, edge: Any) -> _Entity:
    curve = BRepAdaptor_Curve(edge)
    kind = curve.GetType()
    props = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, props)
    entity = _Entity(path, "edge", edge, _CURVES.get(kind, "other"), length=props.Mass())
    length = _quantity("Length", "length", entity.length)
    if kind == GeomAbs_CurveType.GeomAbs_Line:
        entity.line = _Line.of(curve.Line().Position())
        entity.properties = [length, _quantity("Direction", "direction", entity.line.direction)]
    elif kind == GeomAbs_CurveType.GeomAbs_Circle:
        circle = curve.Circle()
        entity.center = _vec(circle.Location())
        r = circle.Radius()
        sweep = abs(curve.LastParameter() - curve.FirstParameter())
        entity.properties = [
            _quantity("Diameter", "length", 2 * r),
            _quantity("Radius", "length", r),
            _quantity("Center", "point", entity.center),
        ]
        if sweep < 2 * math.pi - 1e-9:
            entity.kind = "arc"
            entity.properties += [length, _quantity("Arc angle", "angle", math.degrees(sweep))]
        else:
            entity.properties.append(_quantity("Circumference", "length", entity.length))
    elif kind == GeomAbs_CurveType.GeomAbs_Ellipse:
        ellipse = curve.Ellipse()
        entity.center = _vec(ellipse.Location())
        entity.properties = [
            _quantity("Major radius", "length", ellipse.MajorRadius()),
            _quantity("Minor radius", "length", ellipse.MinorRadius()),
            _quantity("Center", "point", entity.center),
            length,
        ]
    else:
        entity.properties = [length]
    return entity


def _vertex(path: str, vertex: Any) -> _Entity:
    point = _vec(BRep_Tool.Pnt_s(vertex))
    return _Entity(path, "vertex", vertex, "point", [_quantity("Position", "point", point)], center=point)


def _entity(shapes: Shapes, path: str) -> _Entity:
    topo, shape = shapes.entity(path)
    return {"face": _face, "edge": _edge, "vertex": _vertex}[topo](path, shape)


def _minimum(a: _Entity, b: _Entity) -> tuple[float, Vec, Vec]:
    extrema = BRepExtrema_DistShapeShape(a.shape, b.shape)
    if not extrema.IsDone() or extrema.NbSolution() == 0:
        raise MeasureError("the distance between these could not be computed")
    return extrema.Value(), _vec(extrema.PointOnShape1(1)), _vec(extrema.PointOnShape2(1))


def _centers(a: _Entity, b: _Entity, near: Vec) -> tuple[str, tuple[Vec, Vec]] | None:
    """The distance between the centres or axes (the lines of straight edges) of two entities."""
    a_line, b_line = a.axis or a.line, b.axis or b.line
    label = "Perpendicular distance" if a.line is not None or b.line is not None else "Center distance"
    if a.center is not None and b.center is not None:
        return "Center distance", (a.center, b.center)
    if a.center is not None and b_line is not None:
        return label, (a.center, b_line.foot(a.center))
    if a_line is not None and b.center is not None:
        return label, (a_line.foot(b.center), b.center)
    if a_line is not None and b_line is not None:
        return label, _closest(a_line, b_line, near)
    return None


def _normal(a: _Entity, b: _Entity, on_b: Vec) -> tuple[Vec, Vec] | None:
    """The segment from a plane of ``a`` to what ``b`` has parallel to it: a plane, a line, a point (``on_b``, the
    nearest point of ``b``, or its centre)."""
    if a.plane is None:
        return None
    origin, normal = a.plane
    b_line = b.line or b.axis
    if b.plane is not None:
        if _angle(normal, b.plane[1]) > _PARALLEL:
            return None
    elif b_line is not None:
        if abs(_dot(normal, b_line.direction)) > _PARALLEL:
            return None
    elif b.center is not None:
        on_b = b.center
    else:
        return None
    return _sub(on_b, _scale(normal, _dot(_sub(on_b, origin), normal))), on_b


def _pair(a: _Entity, b: _Entity) -> list[dict[str, Any]]:
    distance, on_a, on_b = _minimum(a, b)
    rows = [_quantity("Minimum distance", "length", distance, (on_a, on_b))]
    if distance > _SAME:
        delta = _sub(on_b, on_a)
        rows += [_quantity(f"Δ{axis}", "length", abs(value)) for axis, value in zip("xyz", delta, strict=True)]
    derived: list[tuple[str, tuple[Vec, Vec]]] = []
    centers = _centers(a, b, on_a)
    if centers is not None:
        derived.append(centers)
    normal = _normal(a, b, on_b) or _normal(b, a, on_a)
    if normal is not None:
        derived.append(("Normal distance", normal))
    for label, segment in derived:
        length = _norm(_sub(segment[1], segment[0]))
        if abs(length - distance) > _SAME:
            rows.append(_quantity(label, "length", length, segment))
    a_direction, b_direction = a.direction, b.direction
    if a_direction is not None and b_direction is not None:
        angle = _angle(a_direction[1], b_direction[1])
        # A line and a plane: the angle to the plane, not to its normal.
        if a_direction[0] != b_direction[0]:
            angle = math.pi / 2 - angle
        rows.append(_quantity("Angle", "angle", math.degrees(angle)))
    return rows


def measure(shapes: Shapes, paths: Sequence[str]) -> dict[str, Any]:
    """Each entity's properties, and what relates them: for two, the minimum distance (with its components), the
    distance between their centres or axes and between parallel planes when it differs, the angle between their
    directions; for several edges or faces, their total length or area."""
    if not paths:
        raise MeasureError("nothing selected")
    entities = [_entity(shapes, path) for path in paths]
    results = _pair(*entities) if len(entities) == 2 else []
    edges = [e for e in entities if e.topo == "edge"]
    faces = [e for e in entities if e.topo == "face"]
    if len(edges) > 1:
        results.append(_quantity("Total length", "length", sum(e.length for e in edges)))
    if len(faces) > 1:
        results.append(_quantity("Total area", "area", sum(e.area for e in faces)))
    return {
        "entities": [{"path": e.path, "topo": e.topo, "kind": e.kind, "properties": e.properties} for e in entities],
        "results": results,
    }


def adjacent_faces(shapes: Shapes, path: str) -> list[int] | None:
    """The faces of the same leaf, by their viewer number, that an edge or a vertex bounds; None for a face, or when
    the leaf's faces are not numbered as the mesh's. Sub-shapes keep their identity through ``save``/``load``."""
    topo, shape = shapes.entity(path)
    match = _PATH.fullmatch(path)
    assert match is not None  # entity() matched it
    faces = shapes.leaves[match["leaf"]]["faces"]
    if topo == "face" or faces is None:
        return None
    kind = TopAbs_EDGE if topo == "edge" else TopAbs_VERTEX
    adjacent = []
    for index, face in enumerate(faces):
        explorer = TopExp_Explorer(face, kind)
        while explorer.More():
            if explorer.Current().IsSame(shape):
                adjacent.append(index)
                break
            explorer.Next()
    return adjacent
