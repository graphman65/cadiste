from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from build123d import import_step

from cadiste.cache import TraceCache
from cadiste.server import Builds, Project, _Handler


def _read_project(path: Path) -> dict[str, Any]:
    """Read 3MF geometry independently of the exporter and slicer."""
    import posixpath
    from xml.etree import ElementTree
    from zipfile import ZipFile

    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    production_path = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"
    with ZipFile(path) as archive:
        models = {
            name: ElementTree.fromstring(archive.read(name))
            for name in archive.namelist() if name.endswith(".model")
        }
        settings = ElementTree.fromstring(archive.read("Metadata/model_settings.config"))
    root_path = "3D/3dmodel.model"
    root = models[root_path]
    assert root.attrib["unit"] == "millimeter"
    resources = {
        name: {obj.attrib["id"]: obj for obj in model.findall("m:resources/m:object", ns)}
        for name, model in models.items()
    }
    names = {
        obj.attrib["id"]: metadata.attrib["value"]
        for obj in settings.findall("object")
        for metadata in obj.findall("metadata") if metadata.attrib.get("key") == "name"
    }

    def transformed(vertices, element):
        matrix = [float(value) for value in element.attrib.get(
            "transform", "1 0 0 0 1 0 0 0 1 0 0 0"
        ).split()]
        return [
            tuple(sum(vertex[k] * matrix[k * 3 + axis] for k in range(3)) + matrix[9 + axis]
                  for axis in range(3))
            for vertex in vertices
        ]

    def geometry(model_path, object_id):
        obj = resources[model_path][object_id]
        mesh = obj.find("m:mesh", ns)
        vertices, triangles = [], []
        if mesh is not None:
            vertices = [
                tuple(float(vertex.attrib[axis]) for axis in ("x", "y", "z"))
                for vertex in mesh.findall("m:vertices/m:vertex", ns)
            ]
            triangles = [
                tuple(int(triangle.attrib[key]) for key in ("v1", "v2", "v3"))
                for triangle in mesh.findall("m:triangles/m:triangle", ns)
            ]
        for component in obj.findall("m:components/m:component", ns):
            reference = component.attrib.get(production_path)
            child_path = model_path
            if reference:
                child_path = (reference.lstrip("/") if reference.startswith("/")
                              else posixpath.normpath(posixpath.join(posixpath.dirname(model_path), reference)))
            child_vertices, child_triangles = geometry(child_path, component.attrib["objectid"])
            offset = len(vertices)
            vertices.extend(transformed(child_vertices, component))
            triangles.extend(tuple(index + offset for index in triangle) for triangle in child_triangles)
        return vertices, triangles

    result = []
    for item in root.findall("m:build/m:item", ns):
        object_id = item.attrib["objectid"]
        obj = resources[root_path][object_id]
        vertices, triangles = geometry(root_path, object_id)
        vertices = transformed(vertices, item)
        volume = 0.0
        for triangle in triangles:
            a, b, c = [vertices[index] for index in triangle]
            volume += (
                a[0] * (b[1] * c[2] - b[2] * c[1])
                + a[1] * (b[2] * c[0] - b[0] * c[2])
                + a[2] * (b[0] * c[1] - b[1] * c[0])
            ) / 6
        minimum = [min(vertex[axis] for vertex in vertices) for axis in range(3)]
        maximum = [max(vertex[axis] for vertex in vertices) for axis in range(3)]
        result.append({
            "name": names.get(object_id, obj.attrib.get("name")), "min": minimum,
            "dimensions": [high - low for low, high in zip(minimum, maximum, strict=True)],
            "volume": abs(volume),
        })
    return {"objects": result}


def test_geometry_export_preserves_tiny_shim_instances_and_nested_world_placements(tmp_path: Path):
    from build123d import Box, Compound, Location

    from cadiste.three_mf import export_3mf

    shim = Box(13.6, 6.3, 0.2) - Box(13.1, 4.4, 0.4).moved(Location((0.25, 0, 0)))
    positions = [44, 22, 0, -22, -44]
    instances = []
    for index, y in enumerate(positions):
        instance = shim.moved(Location((0, y - 11, 0)))
        instance.label = f"shim{index}"
        instances.append(instance)
    group = Compound(label="shims", children=instances)
    group.location = Location((0, 11, 0))
    carrier = Box(80, 120, 4).moved(Location((0, 0, -10)))
    carrier.label = "carrier"
    assembly = Compound(label="Assembly", children=[group, carrier])
    assembly.location = Location((100, 0, 7))

    output = tmp_path / "tiny-shims.3mf"
    assert export_3mf(assembly, output)
    objects = _read_project(output)["objects"]
    assert len(objects) == 6
    shims = {obj["name"]: obj for obj in objects if obj["name"].startswith("shim")}
    assert len(shims) == 5
    for index, y in enumerate(positions):
        instance = shims[f"shim{index}"]
        assert instance["dimensions"] == pytest.approx([13.6, 6.3, 0.2], abs=1e-5)
        assert instance["volume"] == pytest.approx(5.608, abs=1e-5)
        assert instance["min"] == pytest.approx([93.2, y - 3.15, 6.9], abs=1e-5)


@pytest.fixture
def slicer_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path]]:
    model = tmp_path / "bambu_model.py"
    model.write_text(
        "from build123d import Box, Compound, Location\n"
        "def build(name):\n"
        "    if name == 'broken':\n"
        "        raise ValueError('unbuildable part')\n"
        "    return Box(10, 10, 10) if name == 'cube' else Box(20, 4, 6)\n"
        "def assembly():\n"
        "    first = build('cube'); first.label = 'cube'\n"
        "    second = build('cube').moved(Location((30, 0, 0))); second.label = 'cube'\n"
        "    bar = build('bar'); bar.label = 'bar'\n"
        "    return Compound(label='Assembly', children=[first, second, bar])\n"
    )
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(tmp_path), os.environ.get("PYTHONPATH", "")])))
    project = Project(
        "bambu_model:build",
        (),
        ("cube", "bar", "broken"),
        assemblies={"Assembly": "bambu_model:assembly"},
        folders={name: [{"name": "parts", "file": True}] for name in ("cube", "bar", "broken")},
    )
    cache = TraceCache(tmp_path / "cache", [tmp_path], "")
    handler = type("SlicerHandler", (_Handler,), {"builds": Builds(project, cache, always_fresh=False)})
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", cache.directory
        finally:
            server.shutdown()
            thread.join()


def _post(url: str, body: object, content_type: str = "application/json") -> tuple[int, Any]:
    request = Request(url + "/api/bambu-studio", data=json.dumps(body).encode(), headers={"Content-Type": content_type})
    try:
        with urlopen(request, timeout=120) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


@pytest.fixture
def bambu_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Consume handed-off geometry in a separate process, without a GUI."""
    executable = tmp_path / "bambustudio"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "from typing import Any\n"
        + inspect.getsource(_read_project)
        + "\npath = Path(sys.argv[1])\n"
        "path.with_suffix('.json').write_text(json.dumps(_read_project(path)))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    return str(executable)


@pytest.fixture
def launcher(
    monkeypatch: pytest.MonkeyPatch, bambu_executable: str,
) -> list[subprocess.Popen[Any]]:
    processes: list[subprocess.Popen[Any]] = []
    popen = subprocess.Popen

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        process = popen(command, **kwargs)
        if len(command) == 2 and command[0] == bambu_executable:
            processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    return processes


def test_slicer_receives_only_shown_geometry_and_each_export_remains_reloadable(
    slicer_server: tuple[str, Path], launcher: list[subprocess.Popen[Any]],
):
    url, cache = slicer_server
    for part in ("cube", "bar"):
        status, _ = _post(url, {"target": {"kind": "file", "path": "parts"}, "parts": [part]})
        assert status == 200
        assert launcher[-1].wait(timeout=10) == 0
    exports = sorted((cache / "bambu-studio").glob("*.3mf"))
    assert len(exports) == 2
    consumed = [json.loads(path.with_suffix(".json").read_text()) for path in exports]
    dimensions = sorted(project["objects"][0]["dimensions"] for project in consumed)
    assert dimensions == [[10, 10, 10], [20, 4, 6]]
    # Both archives survive the HTTP requests and the consuming processes.
    assert all(len(_read_project(path)["objects"]) == 1 for path in exports)
    volumes = sorted(project["objects"][0]["volume"] for project in consumed)
    assert volumes == pytest.approx([480, 1000])


def test_bambu_hidden_paths_keep_only_the_other_duplicate_instance(
    slicer_server: tuple[str, Path], launcher: list[subprocess.Popen[Any]],
):
    url, _ = slicer_server
    status, result = _post(url, {
        "target": {"kind": "assembly", "name": "Assembly"},
        "hidden": ["/Assembly/cube", "/Assembly/bar"],
    })
    assert status == 200
    assert launcher[-1].wait(timeout=10) == 0
    assert Path(result["path"]).suffix == ".3mf"
    objects = json.loads(Path(result["path"]).with_suffix(".json").read_text())["objects"]
    assert len(objects) == 1
    assert objects[0]["name"] == "cube"
    assert objects[0]["volume"] == pytest.approx(1000)
    assert objects[0]["min"][0] == pytest.approx(25)


def test_bambu_preserves_separate_named_repeated_instances(
    slicer_server: tuple[str, Path], launcher: list[subprocess.Popen[Any]],
):
    status, result = _post(slicer_server[0], {"target": {"kind": "assembly", "name": "Assembly"}})
    assert status == 200
    assert launcher[-1].wait(timeout=10) == 0
    objects = json.loads(Path(result["path"]).with_suffix(".json").read_text())["objects"]
    assert len(objects) == 3
    cubes = [obj for obj in objects if obj["name"] == "cube"]
    assert len(cubes) == 2
    assert sorted(obj["min"][0] for obj in cubes) == pytest.approx([-5, 25])
    assert [obj["volume"] for obj in cubes] == pytest.approx([1000, 1000])
    assert all(obj["dimensions"] == [10, 10, 10] for obj in cubes)
    bar = next(obj for obj in objects if obj["name"] == "bar")
    assert bar["dimensions"] == [20, 4, 6]
    assert bar["volume"] == pytest.approx(480)


def test_download_3mf_preserves_filtered_geometry_without_bambu(
    slicer_server: tuple[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from urllib.parse import urlencode

    monkeypatch.setenv("PATH", "")

    query = urlencode([
        ("kind", "assembly"), ("name", "Assembly"), ("format", "3mf"),
        ("hidden", "/Assembly/cube"), ("hidden", "/Assembly/bar"),
    ])
    with urlopen(f"{slicer_server[0]}/api/export?{query}", timeout=120) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "model/3mf"
        assert ".3mf" in response.headers["Content-Disposition"]
        output = tmp_path / "download.3mf"
        output.write_bytes(response.read())
    objects = _read_project(output)["objects"]
    assert len(objects) == 1
    assert objects[0]["volume"] == pytest.approx(1000)
    assert objects[0]["min"][0] == pytest.approx(25)


def test_download_repeated_hidden_paths_filter_geometry(slicer_server: tuple[str, Path], tmp_path: Path):
    from urllib.parse import urlencode

    query = urlencode([
        ("kind", "assembly"), ("name", "Assembly"), ("format", "step"),
        ("hidden", "/Assembly/cube"), ("hidden", "/Assembly/bar"),
    ])
    with urlopen(f"{slicer_server[0]}/api/export?{query}", timeout=120) as response:
        assert response.status == 200
        output = tmp_path / "download.step"
        output.write_bytes(response.read())
    shape = import_step(output)
    assert len(shape.solids()) == 1
    assert pytest.approx(25) == shape.bounding_box().min.X


@pytest.mark.parametrize("hidden", [["/Assembly"], ["/Assembly/missing"]])
def test_hidden_empty_or_unknown_never_launches_slicer(
    slicer_server: tuple[str, Path], launcher: list[subprocess.Popen[Any]], hidden: list[str],
):
    status, body = _post(slicer_server[0], {
        "target": {"kind": "assembly", "name": "Assembly"}, "hidden": hidden,
    })
    assert status != 200
    assert launcher == []


def test_build_failure_reports_traceback_without_starting_slicer(
    slicer_server: tuple[str, Path], launcher: list[subprocess.Popen[Any]]
):
    url, cache = slicer_server
    status, body = _post(url, {"target": {"kind": "part", "name": "broken"}})
    assert status == 500
    assert "ValueError: unbuildable part" in body["log"]
    assert launcher == []
    assert not (cache / "bambu-studio").exists()


def test_missing_local_application_has_actionable_error(
    slicer_server: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PATH", "")
    status, body = _post(slicer_server[0], {"target": {"kind": "part", "name": "cube"}})
    assert status == 503
    assert "Bambu Studio was not found on the server's PATH" in body["error"]


def test_unstartable_application_reports_error_and_discards_failed_handoff(
    slicer_server: tuple[str, Path], bambu_executable: str, monkeypatch: pytest.MonkeyPatch,
):
    popen = subprocess.Popen

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        if len(command) == 2 and command[0] == bambu_executable:
            raise OSError("launcher unavailable")
        return popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    url, cache = slicer_server
    status, body = _post(url, {"target": {"kind": "part", "name": "cube"}})
    assert status == 500
    assert "could not open Bambu Studio:" in body["error"]
    assert list((cache / "bambu-studio").iterdir()) == []


@pytest.mark.parametrize(
    ("body", "content_type", "status"),
    [
        ({"target": {"kind": "part", "name": "cube"}}, "text/plain", 415),
        ({"target": {"kind": "part", "name": "cube"}}, "application/x-www-form-urlencoded", 415),
        ({"target": {"kind": "file", "path": "parts"}, "parts": []}, "application/json", 400),
        ({"target": {"kind": "part", "name": "cube"}, "hidden": "bad"}, "application/json", 400),
        ({"target": {"kind": "part", "name": "cube"}, "hidden": [1]}, "application/json", 400),
        ({"target": {"kind": "part", "name": "cube"}, "hidden": [""]}, "application/json", 400),
        (
            {"target": {"kind": "file", "path": "parts"}, "parts": ["cube"], "hidden": ["/parts/bar"]},
            "application/json", 400,
        ),
        ({"target": {"kind": "assembly", "name": []}}, "application/json", 400),
        ([], "application/json", 400),
    ],
)
def test_unsafe_or_invalid_requests_cannot_launch_app(
    slicer_server: tuple[str, Path],
    launcher: list[subprocess.Popen[Any]],
    body: object,
    content_type: str,
    status: int,
):
    assert _post(slicer_server[0], body, content_type)[0] == status
    assert launcher == []
