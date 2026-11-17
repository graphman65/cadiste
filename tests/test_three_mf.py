from __future__ import annotations

import copy
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from build123d import Box, Compound, Edge, Location
from build123d.topology import Shape

from cadiste.three_mf import export_3mf

_CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
_Point = tuple[float, float, float]
_Triangle = tuple[int, int, int]


def _objects(path: Path) -> list[tuple[str, list[_Point], list[_Triangle]]]:
    """Read actual build objects and mesh indices, independently of the writer."""
    with ZipFile(path) as archive:
        model = ET.fromstring(archive.read("3D/3dmodel.model"))
    assert model.attrib["unit"] == "millimeter"
    resources = {element.attrib["id"]: element for element in model.findall(f"{_CORE}resources/{_CORE}object")}
    objects = []
    for item in model.findall(f"{_CORE}build/{_CORE}item"):
        obj = resources[item.attrib["objectid"]]
        vertices = [
            (float(vertex.attrib["x"]), float(vertex.attrib["y"]), float(vertex.attrib["z"]))
            for vertex in obj.findall(f"{_CORE}mesh/{_CORE}vertices/{_CORE}vertex")
        ]
        if "transform" in item.attrib:
            matrix = [float(value) for value in item.attrib["transform"].split()]
            assert len(matrix) == 12
            vertices = [
                (
                    x * matrix[0] + y * matrix[3] + z * matrix[6] + matrix[9],
                    x * matrix[1] + y * matrix[4] + z * matrix[7] + matrix[10],
                    x * matrix[2] + y * matrix[5] + z * matrix[8] + matrix[11],
                )
                for x, y, z in vertices
            ]
        triangles = [
            (int(triangle.attrib["v1"]), int(triangle.attrib["v2"]), int(triangle.attrib["v3"]))
            for triangle in obj.findall(f"{_CORE}mesh/{_CORE}triangles/{_CORE}triangle")
        ]
        assert vertices and triangles
        edges: Counter[tuple[int, int]] = Counter()
        for triangle in triangles:
            assert len(set(triangle)) == 3
            assert all(0 <= index < len(vertices) for index in triangle)
            a, b, c = triangle
            edges.update(((a, b), (b, c), (c, a)))
        # Every boundary edge has exactly one opposing face: closed, consistent winding.
        assert all(count == 1 and edges[b, a] == 1 for (a, b), count in edges.items())
        objects.append((obj.attrib["name"], vertices, triangles))
    assert len(objects) == len(resources)
    return objects


def _bounds(vertices: list[_Point]) -> tuple[_Point, _Point]:
    return (
        (min(v[0] for v in vertices), min(v[1] for v in vertices), min(v[2] for v in vertices)),
        (max(v[0] for v in vertices), max(v[1] for v in vertices), max(v[2] for v in vertices)),
    )


def _volume(vertices: list[_Point], triangles: list[_Triangle]) -> float:
    volume = 0.0
    for i, j, k in triangles:
        a, b, c = vertices[i], vertices[j], vertices[k]
        volume += (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            + a[1] * (b[2] * c[0] - b[0] * c[2])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        ) / 6
    return volume


def test_nested_rotations_keep_repeated_leaf_instances_and_do_not_mutate_the_tree(tmp_path: Path):
    first = Box(2, 4, 6)
    first.label = "bolt & écrou"
    first.location = Location((0, 2, 0), (0, 0, 90))
    second = copy.copy(first)
    second.location = Location((10, 2, 0), (0, 0, 90))
    arm = Compound(label="arm", children=[first, second])
    arm.location = Location((5, 0, 0), (90, 0, 0))
    root = Compound(label="rig", children=[arm])
    root.location = Location((10, 20, 30), (0, 0, 90))
    nodes = [root, arm, first, second]
    before = [(node.parent, node.children, node.location, node.wrapped) for node in nodes]
    output = tmp_path / "assembly.3mf"

    assert export_3mf(root, output) is True

    objects = _objects(output)
    assert [name for name, _, _ in objects] == ["bolt & écrou", "bolt & écrou"]
    for (_, vertices, triangles), minimum, maximum in zip(
        objects, [(7, 23, 31), (7, 33, 31)], [(13, 27, 33), (13, 37, 33)], strict=True
    ):
        low, high = _bounds(vertices)
        assert low == pytest.approx(minimum, abs=1e-9)
        assert high == pytest.approx(maximum, abs=1e-9)
        assert _volume(vertices, triangles) == pytest.approx(48, abs=1e-9)
    for node, (parent, children, location, wrapped) in zip(nodes, before, strict=True):
        assert node.parent is parent
        assert len(node.children) == len(children)
        assert all(actual is original for actual, original in zip(node.children, children, strict=True))
        assert node.location == location
        assert node.wrapped is wrapped


def test_tiny_u_shim_preserves_geometry_without_slicer_presets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    shim = Box(13.6, 6.3, 0.2) - Box(13.1, 4.4, 0.4).moved(Location((0, 0.95, 0)))
    shim.label = "U-shim & é"
    output = tmp_path / "shim.3mf"
    monkeypatch.setenv("PATH", "")

    export_3mf(shim, output)

    [(name, vertices, triangles)] = _objects(output)
    assert name == "U-shim & é"
    low, high = _bounds(vertices)
    assert tuple(high[axis] - low[axis] for axis in range(3)) == pytest.approx((13.6, 6.3, 0.2), abs=1e-9)
    assert _volume(vertices, triangles) == pytest.approx(5.608, abs=1e-9)
    with ZipFile(output) as archive:
        types = ET.fromstring(archive.read("[Content_Types].xml"))
        defaults = {entry.attrib["Extension"]: entry.attrib["ContentType"] for entry in types}
        assert defaults["model"] == "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"
        assert defaults["rels"] == "application/vnd.openxmlformats-package.relationships+xml"
        relationships = ET.fromstring(archive.read("_rels/.rels"))
        [relationship] = list(relationships)
        assert relationship.attrib["Type"] == "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
        assert relationship.attrib["Target"].lstrip("/") in archive.namelist()
        model = ET.fromstring(archive.read("3D/3dmodel.model"))
        metadata = {entry.attrib["name"]: entry.text for entry in model.findall(f"{_CORE}metadata")}
        assert metadata["Application"] == "Cadiste"
        assert metadata["BambuStudio:3mfVersion"] == "1"
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
        [obj] = config.findall("object")
        object_name = obj.find("metadata[@key='name']")
        assert object_name is not None
        assert object_name.attrib["value"] == name
        [part] = obj.findall("part")
        assert part.attrib["subtype"] == "normal_part"
        part_name = part.find("metadata[@key='name']")
        assert part_name is not None
        assert part_name.attrib["value"] == name
        assert "Metadata/project_settings.config" not in archive.namelist()
        assert [name for name in archive.namelist() if name.endswith(".config")] == ["Metadata/model_settings.config"]
        assert {item.attrib["key"] for item in config.iter("metadata")} == {"name", "matrix"}


def test_multisolid_leaf_stays_one_object_and_not_one_object_per_solid(tmp_path: Path):
    pair = Compound([Box(2, 2, 2), Box(2, 2, 2).moved(Location((10, 0, 0)))])
    pair.label = "pair"
    single = Box(3, 3, 3)
    single.label = "single"
    assembly = Compound(label="assembly", children=[pair, single])
    output = tmp_path / "multisolid.3mf"

    export_3mf(assembly, output)

    objects = _objects(output)
    assert [name for name, _, _ in objects] == ["pair", "single"]
    assert [_volume(vertices, triangles) for _, vertices, triangles in objects] == pytest.approx([16, 27])
    _, vertices, triangles = objects[0]
    low, high = _bounds(vertices)
    assert low == pytest.approx((-1, -1, -1))
    assert high == pytest.approx((11, 1, 1))
    # Disconnected solids remain separate closed components of this one build object.
    assert len(vertices) == 16
    assert len(triangles) == 24
    assert all(
        max(vertices[index][0] for index in triangle) - min(vertices[index][0] for index in triangle) <= 2
        for triangle in triangles
    )


@pytest.mark.parametrize("shape", [Compound(), Edge.make_line((0, 0, 0), (1, 0, 0))])
def test_geometry_without_faces_is_rejected_without_creating_an_archive(tmp_path: Path, shape: Shape):
    output = tmp_path / "empty.3mf"

    with pytest.raises(ValueError, match=r"empty|no faces"):
        export_3mf(shape, output)

    assert not output.exists()
