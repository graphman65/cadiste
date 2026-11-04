"""Which traced operation made each face.

Follows build123d's ``ShapeHistory`` records through a builder's operations, in order. A face the operation left
alone or only rebuilt (a top face trimmed by a hole) keeps the origin it already had; a face it brought in or
created (the hole's wall, a chamfer) is its own. A face brought in from another traced builder's product (an
inserted part, a part object built with its own ``BuildPart``) is followed into that builder, so an origin is a
path from the outer operation to the innermost one.

Some operations keep no record (``split`` in build123d 0.13): a face the record does not account for at all is
matched by geometry instead, and keeps the origin of the face it lies on from before the operation.
"""

from __future__ import annotations

# OCP is a compiled extension without type stubs: pyright cannot see its symbols.
# pyright: reportAttributeAccessIssue=false
from build123d.topology import Shape
from build123d.topology.history import Trace as HistoryTrace
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepTools import BRepTools
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.gp import gp_Pnt, gp_Pnt2d
from OCP.TopAbs import TopAbs_IN, TopAbs_ON
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape

from .trace import BuilderRecord, Op, Trace

type OriginPath = tuple[Op, ...]
"""Outer operation first; the last one made the face."""

_IDENTITY = TopLoc_Location()
_TOLERANCE = 1e-4
"""mm: how far off a face a point may be and still lie on it."""


class _Key:
    """A sub-shape as a dict key with ``IsSame`` semantics: same TShape and Location, any orientation."""

    __slots__ = ("shape",)

    def __init__(self, shape: TopoDS_Shape):
        self.shape = shape

    def __hash__(self) -> int:
        return hash(self.shape)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Key) and self.shape.IsSame(other.shape)


def unlocated_key(shape: TopoDS_Shape) -> _Key:
    """Key matching every placement of one TShape (a product moved by ``Locations`` or ``insert``, an assembly's
    instance of a part)."""
    return _Key(shape.Located(_IDENTITY))


def _source(trace: HistoryTrace, shape: TopoDS_Shape) -> TopoDS_Shape | None:
    """The input sub-shape ``shape`` is, or was rebuilt from, according to ``trace``."""
    if trace.is_untouched(shape):
        return shape
    if trace.is_modified(shape):
        return trace.modified_from.Find(shape)
    return None


class Provenance:
    """Face origins for the builders of one trace, computed on demand and memoised."""

    def __init__(self, trace: Trace):
        self.trace = trace
        self._faces: dict[int, dict[_Key, OriginPath]] = {}
        self._partners: dict[int, dict[_Key, OriginPath]] = {}

    def builder_faces(self, record: BuilderRecord) -> dict[_Key, OriginPath]:
        """Origin of every face of the builder's object after its last operation."""
        if record.index in self._faces:
            return self._faces[record.index]
        origins: dict[_Key, OriginPath] = {}
        for op in record.ops:
            if op.after is None:
                origins = {}
                continue
            step = _Step(op, origins)
            origins = {_Key(face.wrapped): self._origin(face.wrapped, step) for face in op.after.faces()}
        self._faces[record.index] = origins
        return origins

    def _origin(self, face: TopoDS_Shape, step: _Step) -> OriginPath:
        op, before, brought = step.op, step.before, step.brought
        # What was already there wins over what the operation brought: a face merged from both (a box added
        # flush with an existing face) belongs to the older feature.
        earlier = _source(before, face) if before is not None else face
        if earlier is not None and (found := step.origins.get(_Key(earlier))) is not None:
            return found
        tool_face = _source(brought, face) if brought is not None else None
        if tool_face is not None:
            for linked in (op.child, *op.inputs):
                if linked is not None and linked.kind == op.builder.kind:
                    inner = self._partner_faces(linked).get(unlocated_key(tool_face))
                    if inner is not None:
                        return (op, *inner)
            return (op,)
        if any(trace is not None and trace.is_generated(face) for trace in (before, brought)):
            return (op,)
        # The record says nothing about this face: the operation kept none, or the face is new.
        found = step.lies_on_earlier(face)
        return found if found is not None else (op,)

    def _partner_faces(self, record: BuilderRecord) -> dict[_Key, OriginPath]:
        if record.index not in self._partners:
            self._partners[record.index] = {
                unlocated_key(key.shape): path for key, path in self.builder_faces(record).items()
            }
        return self._partners[record.index]

    def face_origins(self, shape: Shape) -> list[OriginPath | None]:
        """Origin of each face of ``shape``, in ``shape.faces()`` order; None where no traced builder made it.

        ``shape`` is normally what the traced build returned. It is matched against the builder its object
        owns, then the outermost builders, latest first: by sub-shape identity, else by TShape (a moved copy).
        A shape with fresh topology (loaded from a BREP cache) matches nothing.
        """
        faces = [face.wrapped for face in shape.faces()]
        builders = self.trace.builders
        candidates = [
            *(b for b in builders if b.owner is shape),
            *(b for b in reversed(builders) if b.parent is None),
            *reversed(builders),
        ]
        best: list[OriginPath | None] = [None] * len(faces)
        best_count = 0
        for record in dict.fromkeys(candidates):
            if not record.ops:
                continue
            same, partners = self.builder_faces(record), self._partner_faces(record)
            found = [same.get(_Key(face)) or partners.get(unlocated_key(face)) for face in faces]
            count = sum(path is not None for path in found)
            if count == len(faces):
                return found
            if count > best_count:
                best, best_count = found, count
        return best


class _Step:
    """One operation being followed: its record, and the origins of the faces it started from."""

    def __init__(self, op: Op, origins: dict[_Key, OriginPath]):
        self.op = op
        self.origins = origins
        history = op.after._history if op.after is not None else None
        self.before = history.trace(history.before) if history is not None else None
        self.brought = history.trace(history.brought) if history is not None else None
        self._boxes: list[tuple[TopoDS_Shape, Bnd_Box]] | None = None

    def lies_on_earlier(self, face: TopoDS_Shape) -> OriginPath | None:
        """Origin of the earlier face an interior point of ``face`` lies on (a piece of a face cut in two)."""
        point = _interior_point(face)
        if point is None:
            return None
        if self._boxes is None:
            self._boxes = []
            for key in self.origins:
                box = Bnd_Box()
                BRepBndLib.Add_s(key.shape, box)
                box.Enlarge(_TOLERANCE)
                self._boxes.append((key.shape, box))
        for earlier, box in self._boxes:
            if not box.IsOut(point) and _lies_on(point, earlier):
                return self.origins[_Key(earlier)]
        return None


def _interior_point(face: TopoDS_Shape) -> gp_Pnt | None:
    """A point strictly inside the face (its centre can be in a hole), from a coarse-to-fine UV grid."""
    topo_face = TopoDS.Face(face)
    u_min, u_max, v_min, v_max = BRepTools.UVBounds_s(topo_face)
    surface = BRep_Tool.Surface_s(topo_face)
    for n in (1, 3, 7):
        for i in range(n):
            for j in range(n):
                u = u_min + (i + 0.5) / n * (u_max - u_min)
                v = v_min + (j + 0.5) / n * (v_max - v_min)
                if BRepClass_FaceClassifier(topo_face, gp_Pnt2d(u, v), _TOLERANCE).State() == TopAbs_IN:
                    return surface.Value(u, v)
    return None


def _lies_on(point: gp_Pnt, face: TopoDS_Shape) -> bool:
    topo_face = TopoDS.Face(face)
    projector = GeomAPI_ProjectPointOnSurf(point, BRep_Tool.Surface_s(topo_face))
    if projector.NbPoints() == 0 or projector.LowerDistance() > _TOLERANCE:
        return False
    u, v = projector.LowerDistanceParameters()
    return BRepClass_FaceClassifier(topo_face, gp_Pnt2d(u, v), _TOLERANCE).State() in (TopAbs_IN, TopAbs_ON)
