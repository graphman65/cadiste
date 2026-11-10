from __future__ import annotations

import json
import math
import struct
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest
from build123d import Box, Color, Compound, Extrinsic, Location, Rectangle, Vector

from cadiste.cli import main
from cadiste.simulation import (
    Collision,
    DrivenVisual,
    Joint,
    Link,
    Simulation,
    Transmission,
    control_config,
    launch_bundle,
    simulation_scene,
    write_bundle,
)


def _robot() -> Simulation:
    return Simulation(
        "crawler",
        (Link("body", Box(20, 30, 40)), Link("wheel", Box(10, 10, 10), pose=Location((40, 0, 0)))),
        (Joint("drive", "body", "wheel", pose=Location((40, 0, 0)), kind="continuous", actuated=True),),
    )


def _model(output: Path, name: str = "crawler") -> ET.Element:
    model = ET.parse(output / "models" / name / "model.sdf").getroot().find("model")
    assert model is not None
    return model


def _floats(text: str | None) -> list[float]:
    assert text is not None
    return [float(value) for value in text.split()]


def test_mass_override_full_tensor_and_com_are_link_local_si(tmp_path: Path):
    shape = Box(10, 20, 30).located(Location((7, -11, 23), (0, 0, 45)))
    link = Link("body", shape, pose=Location((100, 200, 300)), mass=2)
    write_bundle(Simulation("crawler", (link,), ()), tmp_path)
    model = _model(tmp_path)
    inertial = model.find("link/inertial")
    assert inertial is not None
    assert float(inertial.findtext("mass", "")) == pytest.approx(2)
    assert _floats(inertial.findtext("pose")) == pytest.approx([0.007, -0.011, 0.023, 0, 0, 0])
    ixx = 2 * (0.02**2 + 0.03**2) / 12
    iyy = 2 * (0.01**2 + 0.03**2) / 12
    tensor = inertial.find("inertia")
    assert tensor is not None
    assert float(tensor.findtext("ixx", "")) == pytest.approx((ixx + iyy) / 2)
    assert float(tensor.findtext("iyy", "")) == pytest.approx((ixx + iyy) / 2)
    assert float(tensor.findtext("ixy", "")) == pytest.approx((ixx - iyy) / 2)
    assert float(tensor.findtext("izz", "")) == pytest.approx(2 * (0.01**2 + 0.02**2) / 12)
    assert _floats(model.findtext("link/pose")) == pytest.approx([0.1, 0.2, 0.3, 0, 0, 0])
    assert _floats(model.findtext("link/visual/pose")) == pytest.approx(
        [0.007, -0.011, 0.023, 0, 0, math.pi / 4]
    )
    assert model.findtext("link/collision/pose") == model.findtext("link/visual/pose")


def test_mixed_cad_intrinsic_angles_become_sdf_extrinsic_angles(tmp_path: Path):
    pose = Location((1, 2, 3), (23, -34, 45))
    write_bundle(Simulation("crawler", (Link("body", Box(10, 10, 10), pose=pose),), ()), tmp_path)
    values = _floats(_model(tmp_path).findtext("link/pose"))
    reconstructed = Location(
        (values[0] * 1000, values[1] * 1000, values[2] * 1000),
        (math.degrees(values[3]), math.degrees(values[4]), math.degrees(values[5])),
        Extrinsic.XYZ,
    )
    expected = pose.wrapped.Transformation()
    actual = reconstructed.wrapped.Transformation()
    for row in range(1, 4):
        for column in range(1, 5):
            assert actual.Value(row, column) == pytest.approx(expected.Value(row, column))


def test_density_and_mesh_units_are_converted_once(tmp_path: Path):
    write_bundle(Simulation("crawler", (Link("body", Box(10, 20, 30), density=1000),), ()), tmp_path)
    model = _model(tmp_path)
    assert float(model.findtext("link/inertial/mass", "")) == pytest.approx(0.006)
    mesh = model.find("link/visual/geometry/mesh")
    assert mesh is not None
    assert _floats(mesh.findtext("scale")) == [0.001, 0.001, 0.001]
    uri = mesh.findtext("uri", "")
    data = (tmp_path / "models" / uri.removeprefix("model://")).read_bytes()
    triangles = int.from_bytes(data[80:84], "little")
    vertices = []
    for index in range(triangles):
        coordinates = struct.unpack_from("<9f", data, 84 + index * 50 + 12)
        vertices.extend(zip(coordinates[0::3], coordinates[1::3], coordinates[2::3], strict=True))
    assert len(data) == 84 + 50 * triangles
    dimensions = [max(vertex[i] for vertex in vertices) - min(vertex[i] for vertex in vertices) for i in range(3)]
    assert dimensions == pytest.approx([10, 20, 30])


def test_nested_visual_placements_and_child_colors_are_preserved(tmp_path: Path):
    red = Box(10, 10, 10).located(Location((20, 0, 0)))
    red.color = Color("red")
    group = Compound(children=[red]).located(Location((0, 30, 0)))
    blue = Box(5, 5, 5)
    blue.color = Color("blue")
    assembly = Compound(children=[group, blue]).located(Location((0, 0, 40)))
    write_bundle(Simulation("crawler", (Link("body", assembly),), ()), tmp_path)
    visuals = _model(tmp_path).findall("link/visual")
    assert len(visuals) == 2
    assert _floats(visuals[0].findtext("pose")) == pytest.approx([0.02, 0.03, 0.04, 0, 0, 0])
    assert _floats(visuals[1].findtext("pose")) == pytest.approx([0, 0, 0.04, 0, 0, 0])
    assert _floats(visuals[0].findtext("material/diffuse")) == pytest.approx([1, 0, 0, 1])
    assert _floats(visuals[1].findtext("material/diffuse")) == pytest.approx([0, 0, 1, 1])


def test_primitives_joint_frames_passive_springs_and_environment(tmp_path: Path):
    base = _robot()
    collisions = (
        Collision("box", (20, 30, 40), friction=0.8),
        Collision("cylinder", (5, 12), pose=Location((1, 2, 3), (0, 90, 0))),
        Collision("sphere", (4,)),
    )
    body = replace(base.links[0], collisions=collisions)
    passive = Joint(
        "spring", "wheel", "arm", pose=Location((40, 10, 0)), axis=(0, 2, 0),
        lower=-0.4, upper=0.7, spring_stiffness=0.8, spring_reference=-0.1,
    )
    environment = Link("pipe", Box(200, 100, 100), pose=Location((0, 0, -60)))
    robot = replace(base, links=(body, base.links[1], Link("arm", Box(5, 5, 5))),
                    joints=(*base.joints, passive), environment=(environment,))
    launch = write_bundle(robot, tmp_path)
    model = _model(tmp_path)
    assert launch == tmp_path / "sim.launch.py"
    parts = model.findall("link[@name='body']/collision")
    assert _floats(parts[0].findtext("geometry/box/size")) == pytest.approx([0.02, 0.03, 0.04])
    assert float(parts[0].findtext("surface/friction/ode/mu", "")) == 0.8
    assert float(parts[1].findtext("geometry/cylinder/radius", "")) == 0.005
    assert float(parts[1].findtext("geometry/cylinder/length", "")) == 0.012
    assert _floats(parts[1].findtext("pose")) == pytest.approx([0.001, 0.002, 0.003, 0, math.pi / 2, 0])
    assert float(parts[2].findtext("geometry/sphere/radius", "")) == 0.004
    joint = model.find("joint[@name='spring']")
    assert joint is not None
    pose = joint.find("pose")
    assert pose is not None
    assert pose.attrib == {"relative_to": "__model__"}
    assert _floats(joint.findtext("pose")) == pytest.approx([0.04, 0.01, 0, 0, 0, 0])
    assert _floats(joint.findtext("axis/xyz")) == [0, 1, 0]
    assert float(joint.findtext("axis/dynamics/spring_stiffness", "")) == 0.8
    assert float(joint.findtext("axis/dynamics/spring_reference", "")) == -0.1
    controllers = model.findall("plugin[@name='gz::sim::systems::JointController']")
    assert len(controllers) == 1
    assert controllers[0].findtext("use_force_commands") == "true"
    assert float(controllers[0].findtext("cmd_max", "")) == base.joints[0].effort
    assert float(controllers[0].findtext("cmd_min", "")) == -base.joints[0].effort
    assert model.find("link[@name='pipe']") is None
    assert _model(tmp_path, "pipe").findtext("static") == "true"
    world = ET.parse(tmp_path / "world.sdf").getroot()
    assert {item.text for item in world.findall("world/include/uri")} == {"model://crawler", "model://pipe"}
    assert _floats(world.findtext("world/model[@name='ground_plane']/pose"))[2] == pytest.approx(-0.12)
    bridges = json.loads((tmp_path / "bridge.yaml").read_text())
    commands = [bridge for bridge in bridges if bridge["direction"] == "ROS_TO_GZ"]
    assert [command["ros_topic_name"] for command in commands] == ["/crawler/joints/drive/cmd_vel"]
    assert {bridge["gz_topic_name"] for bridge in bridges} >= {"/world/cadiste/clock", "/crawler/joint_states"}


@pytest.mark.parametrize("change,match", [
    (lambda robot: replace(robot, name="../escape"), "simulation name"),
    (lambda robot: replace(robot, links=(robot.links[0], robot.links[0])), "duplicate link"),
    (lambda robot: replace(robot, joints=()), "connected"),
    (lambda robot: replace(robot, joints=(*robot.joints, Joint("back", "wheel", "body", kind="fixed"))), "acyclic"),
    (lambda robot: replace(robot, joints=(replace(robot.joints[0], parent="missing"),)), "distinct robot links"),
    (lambda robot: replace(robot, joints=(replace(robot.joints[0], axis=(0, 0, 0)),)), "axis"),
    (lambda robot: replace(robot, joints=(replace(robot.joints[0], effort=math.nan),)), "effort"),
    (lambda robot: replace(robot, joints=(replace(robot.joints[0], velocity=0),)), "velocity"),
    (lambda robot: replace(robot, joints=(replace(robot.joints[0], velocity_gain=0),)), "gain"),
    (lambda robot: replace(robot, joints=(replace(robot.joints[0], velocity_integral_gain=-1),)), "gain"),
    (lambda robot: replace(robot, joints=(replace(robot.joints[0], kind="revolute"),)), "lower and upper"),
    (lambda robot: replace(robot, joints=(replace(robot.joints[0], kind="revolute", lower=1, upper=2),)), "CAD zero"),
    (lambda robot: replace(robot, links=(replace(robot.links[0], mass=-1), robot.links[1])), "mass"),
    (lambda robot: replace(robot, links=(replace(robot.links[0], density=math.inf), robot.links[1])), "density"),
    (lambda robot: replace(robot, links=(replace(robot.links[0], shape=Rectangle(10, 10)), robot.links[1])), "solid"),
    (lambda robot: replace(
        robot, links=(replace(robot.links[0], collisions=(Collision("sphere", (0,)),)), robot.links[1]),
    ), "dimension"),
    (lambda robot: replace(
        robot, links=(replace(robot.links[0], collisions=(Collision("mesh"),)), robot.links[1]),
    ), "mesh requires"),
])
def test_invalid_physics_and_topology_do_not_write(tmp_path: Path, change, match: str):
    output = tmp_path / "not_created"
    with pytest.raises(ValueError, match=match):
        write_bundle(change(_robot()), output)
    assert not output.exists()


def test_cli_factory_arguments_wrong_type_and_simulation_name_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = '''
from build123d import Box
from cadiste.simulation import Link, Simulation

def build(name):
    return Simulation(name, (Link("body", Box(10, 10, 10)),), ())

def wrong():
    return Box(10, 10, 10)
'''
    (tmp_path / "cadiste_simulation_test_model.py").write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    main(["gazebo", "--output", str(tmp_path / "bundle"), "cadiste_simulation_test_model:build", "rover"])
    assert float(_model(tmp_path / "bundle", "rover").findtext("link/inertial/mass", "")) == pytest.approx(0.0012)
    with pytest.raises(SystemExit):
        main(["gazebo", "--output", str(tmp_path / "wrong"), "cadiste_simulation_test_model:wrong"])
    assert not (tmp_path / "wrong").exists()
    with pytest.raises(SystemExit):
        main(["serve", "cadiste_simulation_test_model:wrong", "--assembly", "Robot=example:build",
              "--simulation", "Other=example:simulation"])



@pytest.mark.parametrize("ratio", [0.4, -0.4, 1.0, -1.0])
def test_motor_metadata_and_native_relay_keep_output_physics_units(tmp_path: Path, ratio: float):
    robot = _robot()
    output_joint = replace(robot.joints[0], velocity=12.0, effort=0.7, velocity_gain=0.12)
    robot = replace(robot, joints=(output_joint,), transmissions=(Transmission("motor", "drive", ratio),))
    write_bundle(robot, tmp_path)
    controls = json.loads((tmp_path / "control.json").read_text())
    assert controls == control_config(robot)
    assert controls["transmissions"] == [{"name": "motor", "joint": "drive", "ratio": ratio}]
    assert controls["feedback"]["joints"] == ["drive"]
    command, = controls["commands"]
    assert command["joint"] == "motor"
    assert command["topic"] == "/crawler/motors/motor/cmd_vel"
    assert command["velocity_limit"] == pytest.approx(12.0 / abs(ratio))
    assert command["effort_limit"] == pytest.approx(0.7 * abs(ratio))
    assert controls["feedback"]["visuals"] == {}
    bridges = json.loads((tmp_path / "bridge.yaml").read_text())
    assert [bridge["ros_topic_name"] for bridge in bridges if bridge["direction"] == "ROS_TO_GZ"] == [
        "/crawler/motors/motor/cmd_vel",
    ]
    model = _model(tmp_path)
    controller = model.find("plugin[@name='gz::sim::systems::JointController']")
    assert controller is not None
    assert controller.findtext("topic") == "/crawler/joints/drive/cmd_vel"
    assert controller.findtext("use_force_commands") == "true"
    assert float(controller.findtext("cmd_min", "")) == -0.7
    assert float(controller.findtext("cmd_max", "")) == 0.7
    assert float(controller.findtext("p_gain", "")) == 0.12
    assert float(model.findtext("joint/axis/limit/velocity", "")) == 12.0
    assert float(model.findtext("joint/axis/limit/effort", "")) == 0.7
    relay = model.find("plugin[@name='cadiste::IdealTransmission']/transmission")
    assert relay is not None
    assert relay.findtext("input_topic") == command["topic"]
    assert relay.findtext("output_topic") == controller.findtext("topic")
    assert float(relay.findtext("ratio", "")) == ratio
    assert float(relay.findtext("velocity_limit", "")) == pytest.approx(command["velocity_limit"])


def _geared_robot() -> Simulation:
    robot = _robot()
    red = Box(3, 5, 7).located(Location((6, 0, 2), (10, 0, 20)))
    red.color = Color("red")
    blue = Box(2, 4, 6).located(Location((-4, 2, 0)))
    blue.color = Color("blue")
    shape = Compound(children=[red, blue]).located(Location((0, 3, 0), (0, 0, 17)))
    return replace(
        robot,
        links=(replace(robot.links[0], pose=Location((100, -40, 70), (17, 23, 31))), robot.links[1]),
        visuals=(DrivenVisual(
            "idler", "body", "drive", shape, Location((120, -10, 90), (30, -20, 45)),
            axis=(0, 1, 0), multiplier=-1.875,
        ),),
    )


def _sdf_location(text: str | None) -> Location:
    values = _floats(text)
    return Location(
        (values[0] * 1000, values[1] * 1000, values[2] * 1000),
        (math.degrees(values[3]), math.degrees(values[4]), math.degrees(values[5])), Extrinsic.XYZ,
    )


def _assert_pose(actual: Location, expected: Location) -> None:
    a = actual.wrapped.Transformation()
    b = expected.wrapped.Transformation()
    for row in range(1, 4):
        for column in range(1, 5):
            assert a.Value(row, column) == pytest.approx(b.Value(row, column), abs=1e-9)


def test_driven_visuals_have_no_physics_and_preserve_leaf_geometry_and_measured_bindings(tmp_path: Path):
    robot = _geared_robot()
    visual, = robot.visuals
    write_bundle(robot, tmp_path)
    model = _model(tmp_path)
    assert {link.get("name") for link in model.findall("link")} == {"body", "wheel"}
    assert {joint.get("name") for joint in model.findall("joint")} == {"drive"}
    parent = model.find("link[@name='body']")
    assert parent is not None
    assert len(parent.findall("collision")) == 1
    binding = model.find("plugin[@name='cadiste::IdealTransmission']/driven_visual")
    assert binding is not None
    assert binding.findtext("parent") == "body"
    assert binding.findtext("joint") == "drive"
    assert float(binding.findtext("multiplier", "")) == visual.multiplier
    assert _floats(binding.findtext("axis")) == [0, 1, 0]
    pivot = _sdf_location(binding.findtext("pose"))
    _assert_pose(robot.links[0].pose * pivot, visual.pose)
    leaves = binding.findall("leaf")
    assert len(leaves) == 2
    for leaf, shape, color in zip(leaves, visual.shape.children, ([1, 0, 0, 1], [0, 0, 1, 1]), strict=True):
        exported = parent.find(f"visual[@name='{leaf.findtext('name')}']")
        assert exported is not None
        offset = _sdf_location(leaf.findtext("pose"))
        _assert_pose(offset, visual.shape.location * shape.location)
        _assert_pose(robot.links[0].pose * _sdf_location(exported.findtext("pose")), visual.pose * offset)
        assert _floats(exported.findtext("material/diffuse")) == color
        mesh = exported.find("geometry/mesh")
        assert mesh is not None
        data = (tmp_path / "models" / mesh.findtext("uri", "").removeprefix("model://")).read_bytes()
        count = struct.unpack_from("<I", data, 80)[0]
        vertices = [
            Vector(*struct.unpack_from("<3f", data, 84 + triangle * 50 + 12 + vertex * 12))
            for triangle in range(count) for vertex in range(3)
        ]
        # Exported STL is leaf-local, not an already-placed CAD shape.
        local_bounds = shape.located(Location()).bounding_box()
        for axis, coordinate in enumerate(("X", "Y", "Z")):
            assert min(tuple(vertex)[axis] for vertex in vertices) == pytest.approx(
                getattr(local_bounds.min, coordinate)
            )
            assert max(tuple(vertex)[axis] for vertex in vertices) == pytest.approx(
                getattr(local_bounds.max, coordinate)
            )
        # Binding composes the spin BEFORE the leaf offset, so an asymmetric
        # off-axis leaf revolves around the declared shaft instead of itself.
        spin = Location((0, 0, 0), visual.axis, math.degrees(0.37 * visual.multiplier))
        moved = robot.links[0].pose * pivot * spin * offset
        _assert_pose(moved, visual.pose * spin * visual.shape.location * shape.location)
    config = control_config(robot)
    assert set(config["feedback"]["links"]) == {"body", "wheel"}
    assert config["feedback"]["visuals"]["idler"]["joint"] == "drive"
    data = config["feedback"]["visuals"]["idler"]["pose"]
    assert data["position"] == pytest.approx([value * 0.001 for value in pivot.position])
    scene = simulation_scene(robot)
    assert set(scene["links"]) == {"body", "wheel", "idler"}
    assert scene["links"]["idler"]["rest"]["position"] == list(visual.pose.position)
    assert len({item["path"] for item in scene["links"].values()}) == 3


@pytest.mark.parametrize("change,match", [
    (lambda r: replace(r, transmissions=(Transmission("motor", "drive", 0),)), "nonzero"),
    (lambda r: replace(r, transmissions=(Transmission("motor", "drive", math.nan),)), "ratio"),
    (lambda r: replace(r, transmissions=(Transmission("motor", "drive", math.inf),)), "ratio"),
    (lambda r: replace(r, transmissions=(Transmission("motor", "missing", 1),)), "actuated"),
    (lambda r: replace(r, transmissions=(Transmission("body", "drive", 1),)), "duplicate"),
    (lambda r: replace(r, transmissions=(Transmission("a", "drive", 1), Transmission("b", "drive", -1))), "multiple"),
    (lambda r: replace(r, joints=(replace(r.joints[0], actuated=False),),
                       transmissions=(Transmission("motor", "drive", 1),)), "actuated"),
    (lambda r: replace(r, transmissions=(Transmission("idler", "drive", 1),)), "duplicate"),
    (lambda r: replace(r, environment=(Link("scenery", Box(5, 5, 5)),),
                       visuals=(replace(r.visuals[0], name="scenery"),)), "duplicate"),
    (lambda r: replace(r, environment=(Link("scenery", Box(5, 5, 5)),),
                       visuals=(replace(r.visuals[0], parent="scenery"),)), "parent"),
    (lambda r: replace(r, visuals=(replace(r.visuals[0], name="drive"),)), "duplicate"),
    (lambda r: replace(r, visuals=(r.visuals[0], r.visuals[0])), "duplicate"),
    (lambda r: replace(r, visuals=(replace(r.visuals[0], parent="missing"),)), "parent"),
    (lambda r: replace(r, visuals=(replace(r.visuals[0], joint="missing"),)), "source joint"),
    (lambda r: replace(r, joints=(replace(r.joints[0], kind="fixed", actuated=False),)), "source joint"),
    (lambda r: replace(r, visuals=(replace(r.visuals[0], axis=(0, 0, 0)),)), "unit vector"),
    (lambda r: replace(r, visuals=(replace(r.visuals[0], axis=(0, 2, 0)),)), "unit vector"),
    (lambda r: replace(r, visuals=(replace(r.visuals[0], axis=(0, 1)),)), "unit vector"),
    (lambda r: replace(r, visuals=(replace(r.visuals[0], axis=(math.nan, 1, 0)),)), "unit vector"),
    (lambda r: replace(r, visuals=(replace(r.visuals[0], multiplier=math.inf),)), "multiplier"),
    (lambda r: replace(r, visuals=(replace(r.visuals[0], pose=None),)), "Location"),
    (lambda r: replace(r, visuals=(replace(r.visuals[0], shape=None),)), "nonempty shape"),
])
def test_invalid_transmission_and_visual_bindings_fail_before_export(tmp_path: Path, change, match: str):
    output = tmp_path / "not_created"
    with pytest.raises(ValueError, match=match):
        write_bundle(change(_geared_robot()), output)
    assert not output.exists()


def test_ordinary_project_has_clean_empty_ideal_schema_without_native_prerequisite(tmp_path: Path):
    robot = _robot()
    write_bundle(robot, tmp_path)
    config = control_config(robot)
    assert config["transmissions"] == []
    assert config["feedback"]["visuals"] == {}
    assert config["commands"][0]["joint"] == "drive"
    assert config["commands"][0]["topic"] == "/crawler/joints/drive/cmd_vel"
    assert _model(tmp_path).find("plugin[@name='cadiste::IdealTransmission']") is None


def test_mixed_transmitted_and_direct_actuators_keep_independent_public_topics(tmp_path: Path):
    robot = _robot()
    robot = replace(
        robot, links=(*robot.links, Link("arm", Box(4, 5, 6))),
        joints=(*robot.joints, Joint("steer", "body", "arm", kind="continuous", actuated=True)),
        transmissions=(Transmission("drive_motor", "drive", -0.4),),
    )
    write_bundle(robot, tmp_path)
    config = control_config(robot)
    assert [(item["joint"], item["topic"]) for item in config["commands"]] == [
        ("drive_motor", "/crawler/motors/drive_motor/cmd_vel"), ("steer", "/crawler/joints/steer/cmd_vel"),
    ]
    assert config["feedback"]["joints"] == ["drive", "steer"]
    bridges = json.loads((tmp_path / "bridge.yaml").read_text())
    assert {item["ros_topic_name"] for item in bridges if item["direction"] == "ROS_TO_GZ"} == {
        "/crawler/motors/drive_motor/cmd_vel", "/crawler/joints/steer/cmd_vel",
    }


@pytest.mark.parametrize("animated_only", [False, True])
def test_ideal_bundle_reports_missing_native_runtime_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, animated_only: bool,
):
    robot = _geared_robot() if animated_only else replace(
        _robot(), transmissions=(Transmission("motor", "drive", -0.4),),
    )
    write_bundle(robot, tmp_path)
    # ROS discovery is independent of the plugin prerequisite under test.
    monkeypatch.setattr("cadiste.simulation.check_runtime", lambda: "/usr/bin/ros2")
    monkeypatch.setenv("AMENT_PREFIX_PATH", str(tmp_path / "empty_prefix"))
    monkeypatch.setenv("GZ_SIM_SYSTEM_PLUGIN_PATH", str(tmp_path / "empty_plugins"))
    with pytest.raises(RuntimeError, match="IdealTransmission Gazebo plugin is unavailable"):
        launch_bundle(tmp_path, gui=False)
    assert not (tmp_path / "gazebo.log").exists()
