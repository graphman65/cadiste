"""Explicit articulated CAD models exported to ROS 2 Jazzy / Gazebo Harmonic.

CAD geometry, primitive sizes and Location translations are millimetres; Location
rotations are degrees. Joint coordinates are radians; mass, density and dynamics
are SI. Link shapes are link-local, while link and joint poses are model-absolute.
A joint coordinate of zero is exactly the supplied CAD pose, not a guessed mate.
Exporting requires no ROS installation. Source factories import these dataclasses
from ``cadiste.simulation`` and return a Simulation to ``cadiste gazebo``.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from build123d import Location, export_stl
from build123d.topology import Shape

# OCP is a compiled extension without type stubs.
# pyright: reportAttributeAccessIssue=false
from OCP.BRepGProp import BRepGProp
from OCP.gp import gp_EulerSequence
from OCP.GProp import GProp_GProps


@dataclass(frozen=True)
class Collision:
    """Primitive dimensions: box xyz, cylinder radius/length (local Z), sphere radius.

    A mesh requires shape. Pose is relative to its link; friction is dimensionless.
    """

    kind: Literal["box", "cylinder", "sphere", "mesh"]
    size: tuple[float, ...] = ()
    pose: Location = field(default_factory=Location)
    shape: Shape | None = None
    friction: float = 1.0


@dataclass(frozen=True)
class Link:
    name: str
    shape: Shape
    pose: Location = field(default_factory=Location)
    mass: float | None = None
    density: float = 1200.0
    collisions: tuple[Collision, ...] = ()


@dataclass(frozen=True)
class Joint:
    """Explicit mate at the CAD zero pose; limits/reference are radians.

    Velocity gains are torque per rad/s (P) and torque per radian (I).
    The integral term and total motor torque are bounded by ``effort``.
    """

    name: str
    parent: str
    child: str
    pose: Location = field(default_factory=Location)
    axis: tuple[float, float, float] = (0, 0, 1)
    kind: Literal["revolute", "continuous", "fixed"] = "revolute"
    lower: float | None = None
    upper: float | None = None
    effort: float = 1.0
    velocity: float = 10.0
    damping: float = 0.01
    friction: float = 0.0
    spring_stiffness: float = 0.0
    spring_reference: float = 0.0
    actuated: bool = False
    velocity_gain: float = 0.01
    velocity_integral_gain: float = 0.0


@dataclass(frozen=True)
class Transmission:
    """Ideal signed output-coordinate / motor-coordinate ratio.

    Joint effort, velocity and controller gains remain in physical output units.
    """

    name: str
    joint: str
    ratio: float


@dataclass(frozen=True)
class DrivenVisual:
    """Visual-only gearing driven by a measured physical joint coordinate.

    ``pose`` is the model-absolute CAD zero spin frame; shape is local to it.
    The spin axis is expressed in that frame, not in the parent link frame.
    """

    name: str
    parent: str
    joint: str
    shape: Shape
    pose: Location
    axis: tuple[float, float, float] = (0, 1, 0)
    multiplier: float = 1.0


@dataclass(frozen=True)
class Simulation:
    name: str
    links: tuple[Link, ...]
    joints: tuple[Joint, ...]
    environment: tuple[Link, ...] = ()
    transmissions: tuple[Transmission, ...] = ()
    visuals: tuple[DrivenVisual, ...] = ()


def _number(value: float, label: str, *, positive: bool = False, nonnegative: bool = False) -> None:
    if not math.isfinite(value) or (positive and value <= 0) or (nonnegative and value < 0):
        requirement = "finite and positive" if positive else "finite and nonnegative" if nonnegative else "finite"
        raise ValueError(f"{label} must be {requirement}, got {value!r}")


def _name(name: str, label: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) or name == "world":
        raise ValueError(
            f"{label} must start with a letter, contain only letters, digits and underscores, and not be 'world'"
        )


def _pose_values(pose: Location) -> tuple[float, ...]:
    # SDF uses extrinsic XYZ (roll, pitch, yaw), unlike build123d's intrinsic XYZ.
    transform = pose.wrapped.Transformation()
    rotation = transform.GetRotation().GetEulerAngles(gp_EulerSequence.gp_Extrinsic_XYZ)
    return (*[coordinate * 0.001 for coordinate in pose.position], *rotation)


def _validate_pose(pose: Location, label: str) -> None:
    if not isinstance(pose, Location):
        raise ValueError(f"{label} must be a build123d Location")
    for value in _pose_values(pose):
        _number(value, label)


def _properties(link: Link) -> tuple[float, tuple[float, ...], tuple[float, ...]]:
    if not isinstance(link.shape, Shape) or not link.shape or link.shape.wrapped.IsNull() or not link.shape.solids():
        raise ValueError(f"link {link.name!r} needs a nonempty solid shape")
    if not link.shape.is_valid:
        raise ValueError(f"link {link.name!r} has an invalid B-rep")
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(link.shape.wrapped, properties)
    volume = properties.Mass()
    _number(volume, f"link {link.name!r} volume", positive=True)
    _number(link.density, f"link {link.name!r} density", positive=True)
    mass = link.mass if link.mass is not None else volume * 1e-9 * link.density
    _number(mass, f"link {link.name!r} mass", positive=True)
    center = properties.CentreOfMass()
    com = (center.X() * 0.001, center.Y() * 0.001, center.Z() * 0.001)
    # OCCT returns the full tensor about COM at unit density, in mm^5.
    # mass / volume converts to kg/mm^3; mm^2 -> m^2 contributes 1e-6.
    matrix = properties.MatrixOfInertia()
    scale = mass / volume * 1e-6
    inertia = tuple(matrix.Value(i, j) * scale for i, j in ((1, 1), (1, 2), (1, 3), (2, 2), (2, 3), (3, 3)))
    for value in (*com, *inertia):
        _number(value, f"link {link.name!r} mass properties")
    if any(inertia[i] <= 0 for i in (0, 3, 5)):
        raise ValueError(f"link {link.name!r} has degenerate inertia")
    return mass, com, inertia


def _validate(simulation: Simulation) -> dict[str, tuple[float, tuple[float, ...], tuple[float, ...]]]:
    if not isinstance(simulation, Simulation):
        raise TypeError(f"expected Simulation, got {type(simulation).__name__}")
    _name(simulation.name, "simulation name")
    if not simulation.links:
        raise ValueError("simulation needs at least one robot link")
    model_names = {simulation.name, "ground_plane"}
    for link in simulation.environment:
        _name(link.name, "environment model name")
        if link.name in model_names:
            raise ValueError(f"duplicate or reserved model name {link.name!r}")
        model_names.add(link.name)
    if simulation.name == "ground_plane":
        raise ValueError("simulation name 'ground_plane' is reserved for the world floor")
    links: dict[str, Link] = {}
    properties = {}
    for link in (*simulation.links, *simulation.environment):
        _name(link.name, "link name")
        if link.name in links:
            raise ValueError(f"duplicate link name {link.name!r}")
        links[link.name] = link
        _validate_pose(link.pose, f"link {link.name!r} pose")
        properties[link.name] = _properties(link)
        for collision in link.collisions:
            label = f"link {link.name!r} collision"
            _validate_pose(collision.pose, f"{label} pose")
            _number(collision.friction, f"{label} friction", nonnegative=True)
            dimensions = {"box": 3, "cylinder": 2, "sphere": 1, "mesh": 0}
            if collision.kind not in dimensions or len(collision.size) != dimensions[collision.kind]:
                raise ValueError(f"{label}: invalid {collision.kind!r} dimensions {collision.size!r}")
            for value in collision.size:
                _number(value, f"{label} dimension", positive=True)
            if collision.kind == "mesh":
                shape = collision.shape
                if (
                    not isinstance(shape, Shape)
                    or not shape
                    or shape.wrapped.IsNull()
                    or not shape.faces()
                    or not shape.is_valid
                ):
                    raise ValueError(f"{label}: mesh requires a valid nonempty shape")
            elif collision.shape is not None:
                raise ValueError(f"{label}: shape is only valid for a mesh")
    robot = {link.name for link in simulation.links}
    names: set[str] = set()
    parents: dict[str, str] = {}
    children: dict[str, list[str]] = {name: [] for name in robot}
    for joint in simulation.joints:
        _name(joint.name, "joint name")
        if joint.name in names or joint.name in robot:
            raise ValueError(f"duplicate joint/frame name {joint.name!r}")
        names.add(joint.name)
        if joint.parent not in robot or joint.child not in robot or joint.parent == joint.child:
            raise ValueError(f"joint {joint.name!r} must connect two distinct robot links")
        if joint.child in parents:
            raise ValueError(f"link {joint.child!r} has multiple parent joints")
        parents[joint.child] = joint.parent
        children[joint.parent].append(joint.child)
        _validate_pose(joint.pose, f"joint {joint.name!r} pose")
        if joint.kind not in ("revolute", "continuous", "fixed"):
            raise ValueError(f"joint {joint.name!r}: unsupported kind {joint.kind!r}")
        if len(joint.axis) != 3 or not all(math.isfinite(value) for value in joint.axis):
            raise ValueError(f"joint {joint.name!r}: axis must have three finite components")
        _number(math.hypot(*joint.axis), f"joint {joint.name!r} axis magnitude", positive=True)
        for name in ("effort", "velocity"):
            _number(getattr(joint, name), f"joint {joint.name!r} {name}", positive=True)
        _number(joint.velocity_gain, f"joint {joint.name!r} velocity gain", positive=True)
        _number(joint.velocity_integral_gain, f"joint {joint.name!r} velocity integral gain", nonnegative=True)
        for name in ("damping", "friction", "spring_stiffness"):
            _number(getattr(joint, name), f"joint {joint.name!r} {name}", nonnegative=True)
        _number(joint.spring_reference, f"joint {joint.name!r} spring reference")
        if joint.kind == "revolute":
            if joint.lower is None or joint.upper is None:
                raise ValueError(f"revolute joint {joint.name!r} needs lower and upper limits in radians")
            _number(joint.lower, f"joint {joint.name!r} lower limit")
            _number(joint.upper, f"joint {joint.name!r} upper limit")
            if not joint.lower <= 0 <= joint.upper or joint.lower >= joint.upper:
                raise ValueError(f"joint {joint.name!r}: limits must be ordered and contain the CAD zero pose")
        elif joint.lower is not None or joint.upper is not None:
            raise ValueError(f"{joint.kind} joint {joint.name!r} must not specify position limits")
        if joint.kind == "fixed" and (joint.actuated or joint.spring_stiffness):
            raise ValueError(f"fixed joint {joint.name!r} cannot be actuated or sprung")
    roots = robot - parents.keys()
    if len(roots) != 1:
        raise ValueError("robot joints must form one connected, rooted, acyclic tree")
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in seen:
            raise ValueError("robot joints contain a cycle")
        seen.add(name)
        pending.extend(children[name])
    if seen != robot:
        raise ValueError("robot joints must form one connected, rooted, acyclic tree")
    joints = {joint.name: joint for joint in simulation.joints}
    occupied = set(links) | names | {simulation.name, "ground_plane"}
    outputs: set[str] = set()
    for transmission in simulation.transmissions:
        _name(transmission.name, "transmission name")
        if transmission.name in occupied:
            raise ValueError(f"duplicate transmission/frame name {transmission.name!r}")
        occupied.add(transmission.name)
        joint = joints.get(transmission.joint)
        if joint is None or not joint.actuated or joint.kind == "fixed":
            raise ValueError(f"transmission {transmission.name!r} requires an actuated physical joint")
        if joint.name in outputs:
            raise ValueError(f"joint {joint.name!r} has multiple transmissions")
        outputs.add(joint.name)
        _number(transmission.ratio, f"transmission {transmission.name!r} ratio")
        if transmission.ratio == 0:
            raise ValueError(f"transmission {transmission.name!r} ratio must be nonzero")
        _number(joint.velocity / abs(transmission.ratio), "motor velocity limit", positive=True)
        _number(joint.effort * abs(transmission.ratio), "motor effort limit", positive=True)
    for visual in simulation.visuals:
        _name(visual.name, "driven visual name")
        if visual.name in occupied:
            raise ValueError(f"duplicate driven visual/frame name {visual.name!r}")
        occupied.add(visual.name)
        if visual.parent not in robot:
            raise ValueError(f"driven visual {visual.name!r} requires a physical robot parent link")
        joint = joints.get(visual.joint)
        if joint is None or joint.kind == "fixed":
            raise ValueError(f"driven visual {visual.name!r} requires a nonfixed physical source joint")
        _validate_pose(visual.pose, f"driven visual {visual.name!r} pose")
        if (
            len(visual.axis) != 3
            or not all(math.isfinite(value) for value in visual.axis)
            or not math.isclose(math.hypot(*visual.axis), 1.0, rel_tol=1e-9, abs_tol=1e-9)
        ):
            raise ValueError(f"driven visual {visual.name!r} axis must be a finite unit vector")
        _number(visual.multiplier, f"driven visual {visual.name!r} multiplier")
        shape = visual.shape
        if (
            not isinstance(shape, Shape) or not shape or shape.wrapped.IsNull()
            or not shape.faces() or not shape.is_valid
        ):
            raise ValueError(f"driven visual {visual.name!r} requires a valid nonempty shape")
    return properties


def _text(parent: ET.Element, name: str, value: object, **attributes: str) -> ET.Element:
    child = ET.SubElement(parent, name, attributes)
    child.text = str(value)
    return child


def _numbers(values: tuple[float, ...]) -> str:
    return " ".join(format(value, ".17g") for value in values)


def _pose(parent: ET.Element, pose: Location, **attributes: str) -> None:
    _text(parent, "pose", _numbers(_pose_values(pose)), **attributes)


def _plugin(parent: ET.Element, system: str, filename: str) -> ET.Element:
    return ET.SubElement(parent, "plugin", name=f"gz::sim::systems::{system}", filename=f"gz-sim-{filename}-system")


def _mesh(parent: ET.Element, shape: Shape, directory: Path, filename: str) -> None:
    # STL retains CAD millimetres; SDF applies the metre conversion exactly once.
    if not export_stl(shape.located(Location()), directory / "meshes" / filename):
        raise RuntimeError(f"could not write simulation mesh {filename}")
    mesh = ET.SubElement(parent, "mesh")
    _text(mesh, "uri", f"model://{directory.name}/meshes/{filename}")
    _text(mesh, "scale", "0.001 0.001 0.001")


def _visuals(
    link: ET.Element, shape: Shape, directory: Path, prefix: str,
    *, pivot: Location | None = None, binding: ET.Element | None = None,
) -> None:
    index = 0

    def walk(node: Shape, above: Location) -> None:
        nonlocal index
        placement = above * node.location
        if node.children:
            for child in node.children:
                walk(child, placement)
            return
        name = f"{prefix}_visual_{index}"
        visual = ET.SubElement(link, "visual", name=name)
        _pose(visual, placement if pivot is None else pivot * placement)
        if binding is not None:
            leaf = ET.SubElement(binding, "leaf")
            _text(leaf, "name", name)
            _pose(leaf, placement)
        _mesh(ET.SubElement(visual, "geometry"), node, directory, f"{prefix}_visual_{index}.stl")
        color = node.color
        if color is not None:
            material = ET.SubElement(visual, "material")
            rgba = _numbers(tuple(color))
            _text(material, "ambient", rgba)
            _text(material, "diffuse", rgba)
        index += 1

    walk(shape, Location())


def _link(parent: ET.Element, link: Link, directory: Path, properties: tuple) -> None:
    element = ET.SubElement(parent, "link", name=link.name)
    _pose(element, link.pose, relative_to="__model__")
    mass, com, inertia = properties
    inertial = ET.SubElement(element, "inertial")
    _text(inertial, "pose", _numbers((*com, 0, 0, 0)))
    _text(inertial, "mass", mass)
    tensor = ET.SubElement(inertial, "inertia")
    for name, value in zip(("ixx", "ixy", "ixz", "iyy", "iyz", "izz"), inertia, strict=True):
        _text(tensor, name, value)
    _visuals(element, link.shape, directory, link.name)
    collisions = link.collisions or (Collision("mesh", shape=link.shape),)
    for index, collision in enumerate(collisions):
        part = ET.SubElement(element, "collision", name=f"collision_{index}")
        placement = collision.pose
        if collision.kind == "mesh":
            assert collision.shape is not None
            placement = placement * collision.shape.location
        _pose(part, placement)
        geometry = ET.SubElement(part, "geometry")
        size = tuple(value * 0.001 for value in collision.size)
        if collision.kind == "mesh":
            assert collision.shape is not None
            _mesh(geometry, collision.shape, directory, f"{link.name}_collision_{index}.stl")
        else:
            primitive = ET.SubElement(geometry, collision.kind)
            if collision.kind == "box":
                _text(primitive, "size", _numbers(size))
            elif collision.kind == "cylinder":
                _text(primitive, "radius", size[0])
                _text(primitive, "length", size[1])
            else:
                _text(primitive, "radius", size[0])
        ode = ET.SubElement(ET.SubElement(ET.SubElement(part, "surface"), "friction"), "ode")
        _text(ode, "mu", collision.friction)
        _text(ode, "mu2", collision.friction)


def _joint(parent: ET.Element, joint: Joint, model_name: str) -> None:
    element = ET.SubElement(
        parent, "joint", name=joint.name, type="revolute" if joint.kind == "continuous" else joint.kind
    )
    _text(element, "parent", joint.parent)
    _text(element, "child", joint.child)
    _pose(element, joint.pose, relative_to="__model__")
    if joint.kind == "fixed":
        return
    axis = ET.SubElement(element, "axis")
    norm = math.hypot(*joint.axis)
    _text(axis, "xyz", _numbers(tuple(value / norm for value in joint.axis)))
    limit = ET.SubElement(axis, "limit")
    if joint.kind == "revolute":
        _text(limit, "lower", joint.lower)
        _text(limit, "upper", joint.upper)
    _text(limit, "effort", joint.effort)
    _text(limit, "velocity", joint.velocity)
    dynamics = ET.SubElement(axis, "dynamics")
    for name in ("damping", "friction", "spring_stiffness", "spring_reference"):
        _text(dynamics, name, getattr(joint, name))
    if joint.actuated:
        controller = _plugin(parent, "JointController", "joint-controller")
        _text(controller, "joint_name", joint.name)
        _text(controller, "topic", f"/{model_name}/joints/{joint.name}/cmd_vel")
        _text(controller, "use_force_commands", "true")
        _text(controller, "p_gain", joint.velocity_gain)
        _text(controller, "i_gain", joint.velocity_integral_gain)
        _text(controller, "i_min", -joint.effort)
        _text(controller, "i_max", joint.effort)
        _text(controller, "cmd_min", -joint.effort)
        _text(controller, "cmd_max", joint.effort)


def _xml(path: Path, root: ET.Element) -> None:
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _model_directory(output: Path, name: str) -> Path:
    directory = output / "models" / name
    (directory / "meshes").mkdir(parents=True, exist_ok=True)
    config = ET.Element("model")
    _text(config, "name", name)
    _text(config, "version", "1.0")
    _text(config, "sdf", "model.sdf", version="1.10")
    _text(config, "description", "Articulated CAD export from Cadiste; SI units in SDF")
    _xml(directory / "model.config", config)
    return directory


_LAUNCH = '''"""Launch this relocatable Cadiste bundle with ROS 2 Jazzy / Gazebo Harmonic."""
import os
import json
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, OpaqueFunction, SetEnvironmentVariable
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def _start(context):
    root = Path(__file__).resolve().parent
    controls = json.loads((root / "control.json").read_text())
    plugin_paths = [Path(path) for path in os.environ.get("GZ_SIM_SYSTEM_PLUGIN_PATH", "").split(os.pathsep) if path]
    plugin_paths += [
        Path(prefix) / "lib" for prefix in os.environ.get("AMENT_PREFIX_PATH", "").split(os.pathsep) if prefix
    ]
    if controls["transmissions"] or controls["feedback"]["visuals"]:
        if not any((path / "libcadiste-ideal-transmission-system.so").is_file() for path in plugin_paths):
            raise RuntimeError(
                "Cadiste IdealTransmission Gazebo plugin is unavailable. Use the Cadiste ROS Docker image "
                "or install libcadiste-ideal-transmission-system.so and add its directory to "
                "GZ_SIM_SYSTEM_PLUGIN_PATH (or its prefix to AMENT_PREFIX_PATH)."
            )
    gui = LaunchConfiguration("gui").perform(context).lower() not in ("false", "0", "no")
    # Quote the world path for ros_gz_sim's command-line parser, including spaces.
    import shlex
    arguments = "-r " + ("" if gui else "-s ") + shlex.quote(str(root / "world.sdf"))
    resource = str(root / "models")
    previous = os.environ.get("GZ_SIM_RESOURCE_PATH")
    if previous:
        resource += os.pathsep + previous
    return [
        SetEnvironmentVariable("GZ_SIM_RESOURCE_PATH", resource),
        SetEnvironmentVariable("GZ_SIM_SYSTEM_PLUGIN_PATH", os.pathsep.join(map(str, plugin_paths))),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                str(Path(get_package_share_directory("ros_gz_sim")) / "launch" / "gz_sim.launch.py")
            ),
            launch_arguments={"gz_args": arguments}.items(),
        ),
        Node(package="ros_gz_bridge", executable="parameter_bridge", name="cadiste_bridge",
             parameters=[{"config_file": str(root / "bridge.yaml"), "use_sim_time": True}], output="screen"),
    ]


def generate_launch_description():
    return LaunchDescription([DeclareLaunchArgument("gui", default_value="true"), OpaqueFunction(function=_start)])
'''


def _pose_data(pose: Location, scale: float = 1.0) -> dict:
    rotation = pose.wrapped.Transformation().GetRotation()
    return {
        "position": [float(coordinate) * scale for coordinate in pose.position],
        "quaternion": [rotation.X(), rotation.Y(), rotation.Z(), rotation.W()],
    }


def _control_config(simulation: Simulation) -> dict:
    transmissions = {item.joint: item for item in simulation.transmissions}
    links = {link.name: link for link in simulation.links}
    commands = []
    for joint in simulation.joints:
        if not joint.actuated:
            continue
        transmission = transmissions.get(joint.name)
        name = transmission.name if transmission else joint.name
        ratio = abs(transmission.ratio) if transmission else 1.0
        category = "motors" if transmission else "joints"
        topic = f"/{simulation.name}/{category}/{name}/cmd_vel"
        commands.append({
            "joint": name, "topic": topic,
            "type": "std_msgs/msg/Float64", "units": "rad/s",
            "effort_limit": joint.effort * ratio,
            "velocity_limit": joint.velocity / ratio,
            "example": f"ros2 topic pub --once {topic} std_msgs/msg/Float64 '{{data: 1.0}}'",
        })
    return {
        "model": simulation.name, "commands": commands,
        "transmissions": [
            {"name": item.name, "joint": item.joint, "ratio": item.ratio}
            for item in simulation.transmissions
        ],
        "joint_states": f"/{simulation.name}/joint_states", "tf": "/tf", "clock": "/clock",
        "launch": "ros2 launch ./sim.launch.py", "runtime": "ROS 2 Jazzy / Gazebo Harmonic",
        "feedback": {
            "world_frame": "cadiste",
            "model_frame": simulation.name,
            "links": {
                link.name: {"frame": f"{simulation.name}/{link.name}", "rest": _pose_data(link.pose, 0.001)}
                for link in simulation.links
            },
            "joints": [joint.name for joint in simulation.joints],
            "visuals": {
                visual.name: {
                    "parent": visual.parent, "joint": visual.joint,
                    "multiplier": visual.multiplier, "axis": list(visual.axis),
                    "pose": _pose_data(links[visual.parent].pose.inverse() * visual.pose, 0.001),
                }
                for visual in simulation.visuals
            },
        },
    }


def control_config(simulation: Simulation) -> dict:
    """The bundle's command metadata, validated without exporting or tessellating meshes."""
    _validate(simulation)
    return _control_config(simulation)


def simulation_scene(simulation: Simulation) -> dict:
    """Explicit robot link nodes and a separate, feedback-independent environment group."""
    from build123d.topology import Compound

    from .mesh import SceneObject, tessellate

    _validate(simulation)
    robot_label = f"Robot ({simulation.name})"
    groups = [
        Compound(label=robot_label, children=[
            Compound(label=link.name, children=[link.shape]).moved(link.pose)
            for link in (*simulation.links, *simulation.visuals)
        ]),
    ]
    if simulation.environment:
        groups.append(Compound(label="Environment", children=[
            Compound(label=link.name, children=[link.shape]).moved(link.pose)
            for link in simulation.environment
        ]))
    assembly = Compound(label=f"{simulation.name} scene", children=groups)
    scene = tessellate([SceneObject(assembly.label, assembly)])
    robot = next(node for node in scene.data["shapes"]["parts"] if node["name"] == robot_label)
    paths = {node["name"]: node["id"] for node in robot["parts"]}
    return {
        "model": simulation.name,
        "controls": _control_config(simulation),
        "scene": {"data": scene.data, "root": scene.data["shapes"]["id"]},
        "links": {
            link.name: {"path": paths[link.name], "rest": _pose_data(link.pose)}
            for link in (*simulation.links, *simulation.visuals)
        },
    }


def write_bundle(simulation: Simulation, output: Path) -> Path:
    """Validate first, write a relocatable bundle, and return its absolute launch path."""
    properties = _validate(simulation)
    output = Path(output).resolve()
    directory = _model_directory(output, simulation.name)
    sdf = ET.Element("sdf", version="1.10")
    model = ET.SubElement(sdf, "model", name=simulation.name)
    for link in simulation.links:
        _link(model, link, directory, properties[link.name])
    for joint in simulation.joints:
        _joint(model, joint, simulation.name)
    if simulation.transmissions or simulation.visuals:
        ideal = ET.SubElement(
            model, "plugin", name="cadiste::IdealTransmission", filename="cadiste-ideal-transmission-system",
        )
        joints = {joint.name: joint for joint in simulation.joints}
        links = {link.name: link for link in simulation.links}
        for transmission in simulation.transmissions:
            entry = ET.SubElement(ideal, "transmission")
            _text(entry, "name", transmission.name)
            _text(entry, "input_topic", f"/{simulation.name}/motors/{transmission.name}/cmd_vel")
            _text(entry, "output_topic", f"/{simulation.name}/joints/{transmission.joint}/cmd_vel")
            _text(entry, "ratio", transmission.ratio)
            _text(entry, "velocity_limit", joints[transmission.joint].velocity / abs(transmission.ratio))
        for visual in simulation.visuals:
            entry = ET.SubElement(ideal, "driven_visual")
            _text(entry, "parent", visual.parent)
            _text(entry, "joint", visual.joint)
            _text(entry, "axis", _numbers(visual.axis))
            _text(entry, "multiplier", visual.multiplier)
            pivot = links[visual.parent].pose.inverse() * visual.pose
            _pose(entry, pivot)
            parent = next(element for element in model.findall("link") if element.get("name") == visual.parent)
            _visuals(parent, visual.shape, directory, visual.name, pivot=pivot, binding=entry)
    state = _plugin(model, "JointStatePublisher", "joint-state-publisher")
    _text(state, "topic", f"/{simulation.name}/joint_states")
    _text(state, "update_rate", 50)
    poses = _plugin(model, "PosePublisher", "pose-publisher")
    _text(poses, "publish_link_pose", "true")
    _text(poses, "publish_model_pose", "true")
    _text(poses, "use_pose_vector_msg", "true")
    _text(poses, "update_frequency", 50)
    _xml(directory / "model.sdf", sdf)
    world_sdf = ET.Element("sdf", version="1.10")
    world = ET.SubElement(world_sdf, "world", name="cadiste")
    physics = ET.SubElement(world, "physics", name="physics", type="ignored")
    _text(physics, "max_step_size", 0.001)
    _text(physics, "real_time_factor", 1)
    _text(world, "gravity", "0 0 -9.81")
    for system, filename in (
        ("Physics", "physics"), ("UserCommands", "user-commands"), ("SceneBroadcaster", "scene-broadcaster")
    ):
        _plugin(world, system, filename)
    light = ET.SubElement(world, "light", name="sun", type="directional")
    _text(light, "pose", "0 0 10 0 0 0")
    _text(light, "diffuse", "0.9 0.9 0.9 1")
    _text(light, "specular", "0.2 0.2 0.2 1")
    _text(light, "direction", "-0.5 0.2 -1")
    _text(light, "cast_shadows", "true")
    for name in (simulation.name, *(link.name for link in simulation.environment)):
        _text(ET.SubElement(world, "include"), "uri", f"model://{name}")
    for link in simulation.environment:
        environment_directory = _model_directory(output, link.name)
        root = ET.Element("sdf", version="1.10")
        environment = ET.SubElement(root, "model", name=link.name)
        _text(environment, "static", "true")
        _link(environment, link, environment_directory, properties[link.name])
        _xml(environment_directory / "model.sdf", root)
    # Keep the CAD origin intact, including pipes centred on it. The floor sits
    # beneath the lowest geometry, rather than cutting a centred pipe in half.
    bottom = min((link.pose * link.shape).bounding_box().min.Z for link in (*simulation.links, *simulation.environment))
    floor = ET.SubElement(world, "model", name="ground_plane")
    _text(floor, "static", "true")
    _text(floor, "pose", f"0 0 {bottom * 0.001 - 0.01:.17g} 0 0 0")
    floor_link = ET.SubElement(floor, "link", name="ground")
    for kind in ("collision", "visual"):
        plane = ET.SubElement(ET.SubElement(ET.SubElement(floor_link, kind, name="ground"), "geometry"), "plane")
        _text(plane, "normal", "0 0 1")
        _text(plane, "size", "100 100")
    _xml(output / "world.sdf", world_sdf)
    bridges = [
        {
            "ros_topic_name": "/clock", "gz_topic_name": "/world/cadiste/clock",
            "ros_type_name": "rosgraph_msgs/msg/Clock", "gz_type_name": "gz.msgs.Clock", "direction": "GZ_TO_ROS",
        },
        {
            "ros_topic_name": f"/{simulation.name}/joint_states",
            "gz_topic_name": f"/{simulation.name}/joint_states",
            "ros_type_name": "sensor_msgs/msg/JointState", "gz_type_name": "gz.msgs.Model", "direction": "GZ_TO_ROS",
        },
        {
            "ros_topic_name": "/tf", "gz_topic_name": f"/model/{simulation.name}/pose",
            "ros_type_name": "tf2_msgs/msg/TFMessage", "gz_type_name": "gz.msgs.Pose_V", "direction": "GZ_TO_ROS",
        },
    ]
    controls = _control_config(simulation)
    for command in controls["commands"]:
        bridges.append({
            "ros_topic_name": command["topic"], "gz_topic_name": command["topic"],
            "ros_type_name": command["type"], "gz_type_name": "gz.msgs.Double", "direction": "ROS_TO_GZ",
        })
    (output / "bridge.yaml").write_text(json.dumps(bridges, indent=2) + "\n")
    (output / "control.json").write_text(json.dumps(controls, indent=2) + "\n")
    launch = output / "sim.launch.py"
    launch.write_text(_LAUNCH)
    return launch


def check_runtime() -> str:
    """Find a sourced ROS runtime and the two required Gazebo integration packages."""
    ros2 = shutil.which("ros2")
    if ros2 is None:
        raise RuntimeError("ROS 2 is unavailable: install ROS 2 Jazzy and source /opt/ros/jazzy/setup.bash")
    for package in ("ros_gz_sim", "ros_gz_bridge"):
        result = subprocess.run([ros2, "pkg", "prefix", package], capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError(
                f"ROS package {package} is unavailable: install ros-jazzy-ros-gz and source its setup.bash"
            )
    return ros2


def launch_bundle(output: Path, *, gui: bool = True) -> subprocess.Popen[bytes]:
    """Start native ros2 launch, with durable output in gazebo.log; caller reaps it."""
    ros2 = check_runtime()
    output = Path(output).resolve()
    launch = output / "sim.launch.py"
    if not launch.is_file():
        raise RuntimeError(f"simulation bundle has no launch file: {launch}")
    controls = json.loads((output / "control.json").read_text())
    if controls["transmissions"] or controls["feedback"]["visuals"]:
        paths = [Path(path) for path in os.environ.get("GZ_SIM_SYSTEM_PLUGIN_PATH", "").split(os.pathsep) if path]
        paths += [
            Path(prefix) / "lib" for prefix in os.environ.get("AMENT_PREFIX_PATH", "").split(os.pathsep) if prefix
        ]
        if not any((path / "libcadiste-ideal-transmission-system.so").is_file() for path in paths):
            raise RuntimeError(
                "Cadiste IdealTransmission Gazebo plugin is unavailable. Use the Cadiste ROS Docker image "
                "or install libcadiste-ideal-transmission-system.so and add its directory to "
                "GZ_SIM_SYSTEM_PLUGIN_PATH (or its prefix to AMENT_PREFIX_PATH)."
            )
    with (output / "gazebo.log").open("ab") as log:
        return subprocess.Popen(
            [ros2, "launch", str(launch), f"gui:={str(gui).lower()}"], stdout=log, stderr=subprocess.STDOUT
        )
