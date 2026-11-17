"""Geometry-only 3MF archives, with one millimetre mesh object per assembly leaf."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from build123d import Location
from build123d.topology import Shape

# OCP is a compiled extension without type stubs.
# pyright: reportAttributeAccessIssue=false
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy

_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"

ET.register_namespace("", _CORE)


def _xml(element: ET.Element) -> bytes:
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def mesh_geometry(
    shape: Shape, location: Location | None = None,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """The exact welded mesh used by the geometry-only exporter, without mutating source topology."""
    copied = Shape.cast(BRepBuilderAPI_Copy(shape.wrapped, True, False).Shape())
    if location is not None:
        copied.location = location
    vertices, triangles = copied.tessellate(0.001, 0.1)
    if not vertices or not triangles:
        raise ValueError("tessellation produced no triangles")
    indices: dict[tuple[float, float, float], int] = {}
    coordinates: list[tuple[float, float, float]] = []
    remap: list[int] = []
    for vertex in vertices:
        point = (vertex.X, vertex.Y, vertex.Z)
        key = (round(vertex.X, 9), round(vertex.Y, 9), round(vertex.Z, 9))
        index = indices.get(key)
        if index is None:
            index = len(indices)
            indices[key] = index
            coordinates.append(point)
        remap.append(index)
    return coordinates, [(remap[a], remap[b], remap[c]) for a, b, c in triangles]


def export_3mf(shape: Shape, path: Path) -> bool:
    """Write standard 3MF geometry and object metadata without supplying printer presets.

    Assembly children are relative placements; flatten their accumulated placements into
    world-space mesh vertices. A compound with no assembly children remains one object,
    including all its solids. Tessellate independent OCCT copies, never the source model.
    The honest producer metadata does not impersonate a native Bambu Studio project.
    """
    model = ET.Element("model", {"unit": "millimeter", "xml:lang": "en-US", "xmlns": _CORE})
    ET.SubElement(model, "metadata", {"name": "Application"}).text = "Cadiste"
    ET.SubElement(model, "metadata", {"name": "BambuStudio:3mfVersion"}).text = "1"
    resources = ET.SubElement(model, "resources")
    build = ET.SubElement(model, "build")
    settings = ET.Element("config")
    count = 0

    def visit(node: Shape, above: Location) -> None:
        nonlocal count
        name = node.label or type(node).__name__
        if not node or node.wrapped.IsNull():
            raise ValueError(f"Cannot export empty 3MF object {name!r}; provide geometry with faces")
        location = above * node.location
        if node.children:
            for child in node.children:
                visit(child, location)
            return
        if not node.faces():
            raise ValueError(f"Cannot export 3MF object {name!r}: no faces to tessellate")

        vertices, triangles = mesh_geometry(node, location)

        count += 1
        object_id = str(count)
        obj = ET.SubElement(resources, "object", {"id": object_id, "type": "model", "name": name})
        mesh = ET.SubElement(obj, "mesh")
        mesh_vertices = ET.SubElement(mesh, "vertices")
        mesh_triangles = ET.SubElement(mesh, "triangles")
        for coordinates in vertices:
            ET.SubElement(
                mesh_vertices, "vertex", dict(zip(("x", "y", "z"), map(str, coordinates), strict=True))
            )
        for triangle in triangles:
            ET.SubElement(
                mesh_triangles, "triangle",
                dict(zip(("v1", "v2", "v3"), map(str, triangle), strict=True)),
            )
        ET.SubElement(build, "item", {"objectid": object_id})
        object_settings = ET.SubElement(settings, "object", {"id": object_id})
        ET.SubElement(object_settings, "metadata", {"key": "name", "value": name})
        part_settings = ET.SubElement(object_settings, "part", {"id": object_id, "subtype": "normal_part"})
        ET.SubElement(part_settings, "metadata", {"key": "name", "value": name})
        ET.SubElement(part_settings, "metadata", {"key": "matrix", "value": "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"})

    visit(shape, Location())
    if not count:
        raise ValueError("Cannot export 3MF: no objects with faces")

    content_types = ET.Element("Types", {"xmlns": _CONTENT_TYPES})
    for extension, content_type in (
        ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
        ("model", "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"),
        ("config", "application/xml"),
    ):
        ET.SubElement(content_types, "Default", {"Extension": extension, "ContentType": content_type})
    relationships = ET.Element("Relationships", {"xmlns": _RELATIONSHIPS})
    ET.SubElement(
        relationships,
        "Relationship",
        {
            "Target": "/3D/3dmodel.model",
            "Id": "rel0",
            "Type": "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel",
        },
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xml(content_types))
        archive.writestr("_rels/.rels", _xml(relationships))
        archive.writestr("3D/3dmodel.model", _xml(model))
        archive.writestr("Metadata/model_settings.config", _xml(settings))
    return True
