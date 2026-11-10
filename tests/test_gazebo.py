from __future__ import annotations

import io
import json
import os
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from struct import unpack_from
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from cadiste.cache import TraceCache
from cadiste.ros import Controls
from cadiste.server import Builds, Project, _Handler


@pytest.fixture
def gazebo_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    model = tmp_path / "gazebo_model.py"
    model.write_text(
        "from build123d import Box, Location\n"
        "from dataclasses import replace\n"
        "from cadiste.simulation import DrivenVisual, Link, Joint, Simulation, Transmission\n"
        "print('model catalog output')\n"
        "def simulation():\n"
        "    return Simulation('test_robot', (Link('base', Box(20, 10, 5)), "
        "Link('wheel', Box(5, 5, 5), pose=Location((20, 0, 0)))), "
        "(Joint('axle', 'base', 'wheel', pose=Location((20, 0, 0)), "
        "kind='continuous', actuated=True, velocity=12.0),), "
        "environment=(Link('support', Box(50, 50, 2), pose=Location((0, 0, -20))),))\n"
        "def broken():\n"
        "    raise ValueError('invalid robot configuration')\n"
        "def different():\n"
        "    return Simulation('positioner', (Link('bed', Box(10, 10, 10)), "
        "Link('table', Box(5, 5, 5))), "
        "(Joint('turntable', 'bed', 'table', kind='continuous', actuated=True, velocity=3.5, effort=0.7),))\n"
        "def transmitted():\n"
        "    return replace(simulation(), transmissions=(Transmission('motor', 'axle', -0.4),), "
        "visuals=(DrivenVisual('idler', 'base', 'axle', Box(3, 4, 5), "
        "Location((8, 1, 2), (0, 30, 0)), multiplier=-1.875),))\n"
    )
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(tmp_path), os.environ.get("PYTHONPATH", "")])))
    project = Project(
        "gazebo_model:unused", (), None,
        assemblies={
            name: "gazebo_model:unused" for name in ("Robot", "Different", "Transmitted", "Broken", "CAD only")
        },
        simulations={
            "Robot": "gazebo_model:simulation", "Different": "gazebo_model:different", "Broken": "gazebo_model:broken",
            "Transmitted": "gazebo_model:transmitted",
        },
    )
    cache = TraceCache(tmp_path / "cache", [tmp_path], "")
    builds = Builds(project, cache, False)
    handler = type(
        "GazeboHandler", (_Handler,),
        {"project": project, "builds": builds, "controls": Controls(builds.control_config)},
    )
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def _request(
    url: str, path: str, body: object | None = None, content_type: str = "application/json"
) -> tuple[int, bytes]:
    request = Request(
        url + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": content_type},
    )
    try:
        with urlopen(request, timeout=120) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def test_gazebo_download_is_an_articulated_model_not_a_flat_cad_mesh(gazebo_server: str):
    status, body = _request(gazebo_server, "/api/gazebo?kind=assembly&name=Robot")
    assert status == 200
    with ZipFile(io.BytesIO(body)) as archive:
        model = ET.fromstring(archive.read("models/test_robot/model.sdf")).find("model")
        assert model is not None
        assert {link.attrib["name"] for link in model.findall("link")} == {"base", "wheel"}
        environment = ET.fromstring(archive.read("models/support/model.sdf")).find("model")
        assert environment is not None and environment.findtext("static") == "true"
        assert {link.attrib["name"] for link in environment.findall("link")} == {"support"}
        joint = model.find("joint")
        assert joint is not None and joint.findtext("parent") == "base" and joint.findtext("child") == "wheel"
        assert [float(value) for value in model.findtext("link[@name='wheel']/pose", "").split()][:3] == [0.02, 0, 0]
        for link in model.findall("link"):
            mesh = link.find("visual/geometry/mesh")
            assert mesh is not None
            uri = mesh.findtext("uri", "")
            data = archive.read("models/" + uri.removeprefix("model://"))
            count = unpack_from("<I", data, 80)[0]
            vertices = [
                unpack_from("<3f", data, 84 + 50 * triangle + 12 + 12 * vertex)
                for triangle in range(count) for vertex in range(3)
            ]
            scale = [float(value) for value in mesh.findtext("scale", "").split()]
            spans = [
                (max(vertex[axis] for vertex in vertices) - min(vertex[axis] for vertex in vertices)) * scale[axis]
                for axis in range(3)
            ]
            assert spans == pytest.approx([0.02, 0.01, 0.005] if link.attrib["name"] == "base" else [0.005] * 3)
        assert "sim.launch.py" in archive.namelist()


def test_gazebo_rejects_geometry_without_an_explicit_simulation_adapter(gazebo_server: str):
    for path in (
        "/api/gazebo?kind=part&name=Robot",
        "/api/gazebo?kind=assembly&name=CAD%20only",
        "/api/gazebo?kind=assembly&name=unknown",
    ):
        assert _request(gazebo_server, path)[0] == 404
    status, body = _request(gazebo_server, "/api/gazebo?kind=assembly&name=Broken")
    assert status == 500
    assert "invalid robot configuration" in json.loads(body)["log"]


def test_gazebo_launch_requires_json_and_reports_missing_ros_environment(
    gazebo_server: str, monkeypatch: pytest.MonkeyPatch
):
    body = {"target": {"kind": "assembly", "name": "Robot"}}
    assert _request(gazebo_server, "/api/gazebo", body, "text/plain")[0] == 415
    assert _request(gazebo_server, "/api/gazebo", {"target": {"name": []}})[0] == 400
    monkeypatch.setenv("PATH", "")
    status, _ = _request(gazebo_server, "/api/gazebo", body)
    assert status == 503


def test_ros_controls_are_discovered_for_each_project_without_a_ros_runtime(gazebo_server: str):
    configurations = []
    for name in ("Robot", "Different"):
        path = f"/api/simulation/controls?kind=assembly&name={name}"
        status, body = _request(gazebo_server, path)
        assert status == 200
        config = json.loads(body)
        configurations.append(config)
        assert json.loads(_request(gazebo_server, path)[1])["config_id"] == config["config_id"]
    first, second = configurations
    assert first["commands"][0]["joint"] == "axle"
    assert first["commands"][0]["topic"] == "/test_robot/joints/axle/cmd_vel"
    assert first["commands"][0]["velocity_limit"] == 12.0
    assert second["commands"][0]["joint"] == "turntable"
    assert second["commands"][0]["topic"] == "/positioner/joints/turntable/cmd_vel"
    assert second["commands"][0]["velocity_limit"] == 3.5
    assert first["config_id"] != second["config_id"]
    assert _request(gazebo_server, "/api/simulation/controls?kind=assembly&name=Broken")[0] == 500
    assert _request(gazebo_server, "/api/simulation/controls?kind=assembly&name=unknown")[0] == 404


def test_ros_http_rejects_unsafe_stale_or_out_of_range_commands_before_publishing(
    gazebo_server: str, monkeypatch: pytest.MonkeyPatch
):
    target = {"kind": "assembly", "name": "Robot"}
    path = "/api/simulation/controls?kind=assembly&name=Robot"
    config = json.loads(_request(gazebo_server, path)[1])
    command = {"target": target, "config_id": config["config_id"], "values": {"axle": 1.0}}
    assert _request(gazebo_server, "/api/simulation/command", command, "text/plain")[0] == 415
    assert _request(gazebo_server, "/api/simulation/command", {**command, "config_id": "old"})[0] == 409
    for values in ({"axle": 12.1}, {"axle": float("nan")}, {"axle": True}, {}):
        assert _request(gazebo_server, "/api/simulation/command", {**command, "values": values})[0] == 400
    assert _request(
        gazebo_server, "/api/simulation/command", {**command, "values": {"axle": 0, "unconfigured": 1}},
    )[0] == 404
    assert _request(
        gazebo_server, "/api/simulation/command", {**command, "target": {"kind": "assembly", "name": "Different"}},
    )[0] == 409
    monkeypatch.setenv("PATH", "")
    assert _request(gazebo_server, "/api/simulation/command", command)[0] == 503


def test_ros_http_invalidates_old_controls_after_metadata_changes(gazebo_server: str, tmp_path: Path):
    path = "/api/simulation/controls?kind=assembly&name=Robot"
    previous = json.loads(_request(gazebo_server, path)[1])
    model = tmp_path / "gazebo_model.py"
    model.write_text(model.read_text().replace("velocity=12.0", "velocity=8.0"))
    current = json.loads(_request(gazebo_server, path)[1])
    assert current["commands"][0]["velocity_limit"] == 8.0
    assert previous["config_id"] != current["config_id"]
    command = {"target": current["target"], "config_id": previous["config_id"], "values": {"axle": 0}}
    assert _request(gazebo_server, "/api/simulation/command", command)[0] == 409


def test_live_scene_uses_explicit_link_nodes_and_requires_approved_configuration(gazebo_server: str):
    controls_path = "/api/simulation/controls?kind=assembly&name=Robot"
    config = json.loads(_request(gazebo_server, controls_path)[1])
    scene_path = f"/api/simulation/scene?kind=assembly&name=Robot&config_id={config['config_id']}"
    status, body = _request(gazebo_server, scene_path)
    assert status == 200
    data = json.loads(body)
    groups = data["scene"]["data"]["shapes"]["parts"]
    robot = next(group for group in groups if data["links"]["base"]["path"].startswith(group["id"] + "/"))
    environment, = (group for group in groups if group is not robot)
    children = {node["id"]: node for node in robot["parts"]}
    assert set(data["links"]) == {"base", "wheel"}
    assert {binding["path"] for binding in data["links"].values()} == set(children)
    support, = environment["parts"]
    assert support["loc"][0] == [0.0, 0.0, -20.0]
    assert support["id"] not in {binding["path"] for binding in data["links"].values()}
    wheel = children[data["links"]["wheel"]["path"]]
    assert wheel["loc"][0] == [20.0, 0.0, 0.0]
    assert data["links"]["wheel"]["rest"]["position"] == [20.0, 0.0, 0.0]
    assert config["feedback"]["links"]["wheel"]["rest"]["position"] == [0.02, 0.0, 0.0]
    for endpoint in ("scene", "feedback"):
        path = f"/api/simulation/{endpoint}?kind=assembly&name=Robot&config_id=old"
        assert _request(gazebo_server, path)[0] == 409


def test_live_scene_rejects_factory_changes_instead_of_binding_stale_geometry(gazebo_server: str, tmp_path: Path):
    config = json.loads(_request(gazebo_server, "/api/simulation/controls?kind=assembly&name=Robot")[1])
    model = tmp_path / "gazebo_model.py"
    model.write_text(model.read_text().replace("Location((20, 0, 0))", "Location((30, 0, 0))"))
    path = f"/api/simulation/scene?kind=assembly&name=Robot&config_id={config['config_id']}"
    assert _request(gazebo_server, path)[0] == 409


def test_transmitted_bundle_exposes_only_motor_commands_and_source_driven_visual_scene(gazebo_server: str):
    status, body = _request(gazebo_server, "/api/simulation/controls?kind=assembly&name=Transmitted")
    assert status == 200
    config = json.loads(body)
    assert config["transmissions"] == [{"name": "motor", "joint": "axle", "ratio": -0.4}]
    command, = config["commands"]
    assert command["joint"] == "motor"
    assert command["topic"] == "/test_robot/motors/motor/cmd_vel"
    assert command["velocity_limit"] == 30.0
    assert command["effort_limit"] == 0.4
    assert config["feedback"]["joints"] == ["axle"]
    assert set(config["feedback"]["links"]) == {"base", "wheel"}
    assert config["feedback"]["visuals"]["idler"] == {
        "parent": "base", "joint": "axle", "axis": [0, 1, 0], "multiplier": -1.875,
        "pose": {"position": [0.008, 0.001, 0.002], "quaternion": pytest.approx([0, 0.2588190451, 0, 0.9659258263])},
    }
    status, body = _request(
        gazebo_server, f"/api/simulation/scene?kind=assembly&name=Transmitted&config_id={config['config_id']}",
    )
    assert status == 200
    scene = json.loads(body)
    assert set(scene["links"]) == {"base", "wheel", "idler"}
    assert scene["links"]["idler"]["rest"]["position"] == [8, 1, 2]
    status, body = _request(gazebo_server, "/api/gazebo?kind=assembly&name=Transmitted")
    assert status == 200
    with ZipFile(io.BytesIO(body)) as archive:
        bridges = json.loads(archive.read("bridge.yaml"))
        assert [item["ros_topic_name"] for item in bridges if item["direction"] == "ROS_TO_GZ"] == [
            "/test_robot/motors/motor/cmd_vel",
        ]
        model = ET.fromstring(archive.read("models/test_robot/model.sdf")).find("model")
        assert model is not None
        assert {link.get("name") for link in model.findall("link")} == {"base", "wheel"}
        binding = model.find("plugin[@name='cadiste::IdealTransmission']/driven_visual")
        assert binding is not None and binding.findtext("joint") == "axle"
        leaf, = binding.findall("leaf")
        assert model.find(f"link[@name='base']/visual[@name='{leaf.findtext('name')}']") is not None
    # Old physical command names are not public aliases for virtual motors.
    payload = {"target": config["target"], "config_id": config["config_id"], "values": {"axle": 0}}
    assert _request(gazebo_server, "/api/simulation/command", payload)[0] == 404
