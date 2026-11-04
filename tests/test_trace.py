from __future__ import annotations

from build123d import (
    Axis,
    BasePartObject,
    Box,
    BuildLine,
    BuildPart,
    BuildSketch,
    Circle,
    Cylinder,
    GeomType,
    Keep,
    Line,
    Locations,
    Mode,
    Part,
    Plane,
    Polygon,
    Rectangle,
    Sketch,
    chamfer,
    extrude,
    insert,
    make_face,
    revolve,
    split,
)

from cadiste import Group, OpNode, Provenance, feature_tree, tracing


def _origin_of(select) -> list[str]:
    """Kinds along the origin path of the one face of ``part_builder()`` that ``select`` picks."""
    with tracing() as trace:
        built = part_builder()
    faces = built.faces()
    (index,) = [i for i, face in enumerate(faces) if select(face)]
    path = Provenance(trace).face_origins(built)[index]
    assert path is not None
    return [op.kind for op in path]


def drill(radius: float) -> None:
    Cylinder(radius, 20, mode=Mode.SUBTRACT)


def part_builder() -> Part:
    with BuildPart() as block:
        Box(10, 10, 10)
        drill(2)
        chamfer(block.edges().filter_by(GeomType.CIRCLE).group_by(Axis.Z)[-1], 0.5)
    assert block.part is not None
    return block.part


def test_a_trimmed_face_keeps_its_feature_and_new_faces_belong_to_the_operation_that_made_them():
    # The top face is cut by the hole and then by the chamfer, and still belongs to the box.
    assert _origin_of(lambda f: f.geom_type == GeomType.PLANE and f.center().Z > 4.9) == ["Box"]
    assert _origin_of(lambda f: f.geom_type == GeomType.CYLINDER) == ["Cylinder"]
    assert _origin_of(lambda f: f.geom_type == GeomType.CONE) == ["chamfer"]


def test_operations_are_grouped_under_the_function_call_that_made_them():
    with tracing() as trace:
        part_builder()
    (root,) = [b for b in trace.builders if b.parent is None]
    children = feature_tree(root).children
    assert [type(child) for child in children] == [OpNode, Group, OpNode]
    group = children[1]
    assert isinstance(group, Group) and group.site.label == "drill"
    assert [node.op.kind for node in group.children if isinstance(node, OpNode)] == ["Cylinder"]


def test_a_private_sketch_is_linked_to_the_operation_that_consumed_it():
    with tracing() as trace:
        with BuildPart():
            with BuildSketch(mode=Mode.PRIVATE) as profile:
                Rectangle(4, 2)
            assert isinstance(profile.sketch, Sketch)
            extrude(profile.sketch, 3)
    (extrusion,) = [op for op in trace.ops if op.kind == "extrude"]
    assert [b.name for b in extrusion.inputs] == ["profile"]


def test_a_sketch_element_is_placed_where_build123d_put_the_sketch():
    with tracing() as trace:
        with BuildPart():
            with BuildSketch(Plane.XZ.offset(-2), mode=Mode.PRIVATE) as profile:
                with Locations((5, 3)):
                    Circle(1)
            assert isinstance(profile.sketch, Sketch)
            extrude(profile.sketch, 1)
    (sketch,) = [record for record in trace.builders if record.kind == "BuildSketch"]
    (circle,) = sketch.ops
    assert circle.obj is not None
    records = {id(record.builder): record for record in trace.builders}

    placed = sketch.placed(circle.obj, records)

    assert placed is not None
    expected = profile.sketch.faces()[0].center()
    assert (placed.faces()[0].center() - expected).length < 1e-9
    assert (circle.obj.faces()[0].center() - expected).length > 1  # the element itself is in sketch coordinates


def test_arguments_are_named_after_parameters_with_the_expressions_written_in_the_call():
    depth = 3
    corners = [(0, 0), (1, 0), (0, 1)]
    with tracing() as trace:
        with BuildPart():
            Box(10, 2 * depth, height=depth)
            with BuildSketch(mode=Mode.PRIVATE) as profile:
                Polygon(*corners)
            assert isinstance(profile.sketch, Sketch)
            extrude(profile.sketch, amount=depth)
    box, polygon, extrusion = trace.ops

    # A builder object binds to its __init__, not to the metaclass __call__(*args, **kwargs).
    assert [(a.name, a.source, a.value) for a in box.arguments] == [
        ("length", "10", "10"),
        ("width", "2 * depth", "6"),
        ("height", "depth", "3"),
    ]
    # A *starred call hides which expression each positional argument is.
    assert [(a.name, a.source, a.value) for a in polygon.arguments] == [("pts", "", "((0, 0), (1, 0), (0, 1))")]
    # An operation function lists its parameters; the ones not written in the call have no expression.
    written = {a.name: a.source for a in extrusion.arguments}
    assert written["to_extrude"] == "profile.sketch" and written["amount"] == "depth" and written["dir"] == ""


class Peg(BasePartObject):
    """A part object with its own builder, like a CAD library's custom parts."""

    def __init__(self, mode: Mode = Mode.ADD):
        with BuildPart() as body:
            Box(6, 6, 6)
            Cylinder(1, 10, mode=Mode.SUBTRACT)
        assert body.part is not None
        super().__init__(part=body.part, mode=mode)


def test_faces_of_an_inserted_part_lead_into_its_builder_and_split_owns_only_its_cut():
    with tracing() as trace:
        with BuildPart() as half:
            insert(Peg(mode=Mode.PRIVATE))
            split(bisect_by=Plane.XY, keep=Keep.TOP)
    assert half.part is not None
    faces = half.part.faces()
    origins = Provenance(trace).face_origins(half.part)
    by_face = {i: [op.kind for op in path] for i, path in enumerate(origins) if path is not None}
    assert len(by_face) == len(faces)

    (bore,) = [i for i, f in enumerate(faces) if f.geom_type == GeomType.CYLINDER]
    assert by_face[bore] == ["insert", "Cylinder"]
    # split keeps no history: the half-height side faces are matched to the box faces they are pieces of.
    sides = [i for i, f in enumerate(faces) if f.geom_type == GeomType.PLANE and abs(f.normal_at().Z) < 1e-6]
    assert len(sides) == 4 and all(by_face[i] == ["insert", "Box"] for i in sides)
    (cut,) = [i for i, f in enumerate(faces) if f.geom_type == GeomType.PLANE and abs(f.center().Z) < 1e-6]
    assert by_face[cut] == ["split"]

    (inserting,) = [op for op in trace.ops if op.kind == "insert"]
    assert [b.kind for b in inserting.inputs] == ["BuildPart"]  # the peg's body, not anything nested in it


def revolved_profile() -> Part:
    with BuildPart() as roller:
        with BuildSketch(Plane.XZ):
            with BuildLine():
                Line((1, 0), (3, 0))
                Line((3, 0), (3, 4))
                Line((3, 4), (1, 0))
            make_face()
        revolve(axis=Axis.Z)
    assert roller.part is not None
    return roller.part


def test_tracing_does_not_change_what_nested_builders_build():
    # A BuildLine hands its edges to the enclosing BuildSketch only if build123d sees them in the same scope.
    untraced = revolved_profile().volume
    with tracing():
        traced = revolved_profile().volume
    assert abs(traced - untraced) < 1e-9
