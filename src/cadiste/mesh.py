"""Meshes for the viewer: shapes tessellated by ocp_tessellate into the format three-cad-viewer renders (the same
pipeline OCP CAD Viewer uses, so models look the same)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from build123d.topology import Shape

# OCP is a compiled extension without type stubs: pyright cannot see its symbols.
# pyright: reportAttributeAccessIssue=false
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape
from ocp_tessellate.convert import combined_bb, tessellate_group, to_ocpgroup
from ocp_tessellate.ocp_utils import (
    get_edges,
    get_faces,
    get_vertices,
    is_degenerated_edge,
    mul_locations,
    tq_to_loc,
)
from ocp_tessellate.utils import numpy_to_buffer_json

_TESSELLATION = {"deviation": 0.1, "angular_tolerance": 0.2, "render_edges": True, "render_normals": False}
"""OCP CAD Viewer's defaults."""


@dataclass(frozen=True)
class SceneObject:
    name: str
    """Unique among the scene's objects: the viewer shows it in its tree and paths it ``/Group/<name>``."""
    shape: Shape
    color: str | None = None
    """CSS colour; None keeps the shape's own colour (or ocp_tessellate's default)."""
    alpha: float | None = None


@dataclass(frozen=True)
class Leaf:
    """A leaf of the viewer's shape tree with its faces, edges and vertices placed where the viewer draws them, in
    the viewer's numbering: its ``<path>/faces/faces_<i>`` is ``faces[i]``. A list is None when the mesh does not
    hold exactly the shape's (a face that did not triangulate is left out of the mesh, shifting the numbers)."""

    path: str
    faces: list[TopoDS_Shape] | None
    edges: list[TopoDS_Shape] | None
    vertices: list[TopoDS_Shape] | None


@dataclass(frozen=True)
class Scene:
    data: dict[str, Any]
    """The viewer's ``{instances, shapes}``: pass it to ``Viewer.render`` as is."""
    paths: dict[str, str]
    """Viewer path of each object, by name."""
    faces: dict[str, int]
    """Faces in the mesh of each object tessellated as one leaf (an assembly is not): the viewer numbers them
    ``<path>/faces/faces_<i>`` in ``shape.faces()`` order."""
    leaves: list[Leaf]
    """Every leaf of the shape tree (an assembly's instances one by one), for measuring."""


def _counted(items: list[TopoDS_Shape], count: int) -> list[TopoDS_Shape] | None:
    return items if len(items) == count else None


def _leaves(shapes: dict[str, Any], mapping: dict[str, Any], meshed: list[dict[str, Any]]) -> list[Leaf]:
    """The leaves of the tessellated tree, placed the way the viewer places them: each node's ``loc`` composed from
    the root, applied to the object ocp_tessellate meshed for it (an instance without its location, or the edges and
    vertices as given)."""
    objects: dict[str, Any] = {}

    def index(node: dict[str, Any]) -> None:
        if "parts" in node:
            for child in node["parts"]:
                index(child)
        else:
            objects[node["id"]] = node["shape"]

    index(mapping)
    leaves: list[Leaf] = []

    def walk(node: dict[str, Any], above: TopLoc_Location | None) -> None:
        loc = above if node.get("loc") is None else mul_locations(above, tq_to_loc(*node["loc"]))

        def place(shape: TopoDS_Shape) -> TopoDS_Shape:
            return shape if loc is None else shape.Moved(loc)

        if "parts" in node:
            for child in node["parts"]:
                walk(child, loc)
            return
        source = objects.get(node["id"])
        if source is None:
            return
        if node["type"] == "shapes":
            shape = place(source["obj"])
            mesh = meshed[node["shape"]["ref"]]
            faces = _counted(list(get_faces(shape)), len(mesh["triangles_per_face"]))
            # Only edges bounding a face are drawn on a meshed shape.
            edges = _counted([edge for edge, _ in get_edges(shape, True)], len(mesh["segments_per_edge"]))
            vertices = _counted(list(get_vertices(shape)), len(mesh["obj_vertices"]) // 3)
        else:
            mesh = node["shape"]
            if node["type"] == "edges":
                # As ocp_tessellate discretizes them: degenerated edges are skipped, the vertices met in edge order.
                drawn = [edge for edge in source if not is_degenerated_edge(edge)]
                seen: list[TopoDS_Shape] = []
                for edge in drawn:
                    for vertex in get_vertices(edge):
                        if vertex not in seen:
                            seen.append(vertex)
                edges = _counted([place(edge) for edge in drawn], len(mesh["segments_per_edge"]))
            else:
                seen, edges = list(source), []
            faces = []
            vertices = _counted([place(vertex) for vertex in seen], len(mesh["obj_vertices"]) // 3)
        leaves.append(Leaf(node["id"], faces, edges, vertices))

    walk(shapes, None)
    return leaves


def tessellate(objects: Sequence[SceneObject]) -> Scene:
    group, instances = to_ocpgroup(
        *(o.shape for o in objects),
        names=[o.name for o in objects],
        # None keeps the shape's own colour and alpha: an explicit alpha would override every child of an assembly.
        colors=[o.color for o in objects],
        alphas=[o.alpha for o in objects],
    )
    meshed, shapes, mapping = tessellate_group(group, instances, dict(_TESSELLATION))
    if shapes.get("bb") is not None:
        shapes["bb"] = combined_bb(shapes).to_dict()  # numpy scalars -> floats
    paths: dict[str, str] = {}
    faces: dict[str, int] = {}
    for node in shapes["parts"]:
        paths[node["name"]] = node["id"]
        reference = node.get("shape")
        if "parts" not in node and isinstance(reference, dict) and "ref" in reference:
            faces[node["name"]] = len(meshed[reference["ref"]]["triangles_per_face"])
    leaves = _leaves(shapes, mapping, meshed)
    data = numpy_to_buffer_json({"instances": meshed, "shapes": shapes})
    assert isinstance(data, dict)  # a dict in, a dict out: only the arrays inside are encoded
    return Scene(data, paths, faces, leaves)
