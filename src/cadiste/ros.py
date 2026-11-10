"""Validated, explicitly requested ROS velocity commands, without importing CAD or rclpy.

Each Controls instance belongs to one project. A describe captures an adapter's
metadata; sends can only use that exact snapshot. Per-target FIFO serialization
keeps a Stop behind every older Send, including all its concurrent publishers.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

_CONFIG_FIELDS = {
    "model", "commands", "joint_states", "tf", "clock", "launch", "runtime", "feedback", "transmissions",
}
_COMMAND_FIELDS = {"joint", "topic", "type", "units", "effort_limit", "velocity_limit", "example"}
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_TOPIC = re.compile(r"/[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*")
_PUBLISH_TIMEOUT = 10
_MAX_PUBLISHERS = 8


class ConfigConflictError(ValueError):
    """The client has not loaded this target, or is using another config snapshot."""


class InvalidConfigError(ValueError):
    """An adapter returned unsupported or malformed control metadata."""


@dataclass(frozen=True)
class _Command:
    topic: str
    velocity_limit: float


@dataclass(frozen=True)
class _Snapshot:
    config_id: str
    metadata: str
    commands: dict[str, _Command]


@dataclass
class _Target:
    condition: threading.Condition = field(default_factory=threading.Condition)
    next_ticket: int = 0
    serving: int = 0
    snapshot: _Snapshot | None = None

    @contextmanager
    def serialized(self) -> Iterator[None]:
        with self.condition:
            ticket = self.next_ticket
            self.next_ticket += 1
            self.condition.wait_for(lambda: self.serving == ticket)
        try:
            yield
        finally:
            with self.condition:
                self.serving += 1
                self.condition.notify_all()


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _topic(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not _TOPIC.fullmatch(value)
        or "__" in value
        or len(value) > 255
    ):
        raise InvalidConfigError(f"{label} must be a valid absolute ROS topic name")
    return value


def _pose_metadata(value: Any, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {"position", "quaternion"}:
        raise InvalidConfigError(f"{label} needs a position and quaternion")
    for key, size in (("position", 3), ("quaternion", 4)):
        values = value[key]
        if not isinstance(values, list) or len(values) != size or not all(_finite(item) for item in values):
            raise InvalidConfigError(f"{label} has invalid {key}")
    if not math.isclose(math.hypot(*value["quaternion"]), 1.0, abs_tol=1e-6):
        raise InvalidConfigError(f"{label} needs a unit quaternion")


def _feedback_metadata(value: Any, model: str) -> None:
    if not isinstance(value, dict) or set(value) != {"world_frame", "model_frame", "links", "joints", "visuals"}:
        raise InvalidConfigError("feedback must declare world/model frames, physical links/joints and driven visuals")
    if value["model_frame"] != model:
        raise InvalidConfigError("feedback model frame must match its simulation")
    if not isinstance(value["world_frame"], str):
        raise InvalidConfigError("feedback world frame must be a string")
    _topic("/" + value["world_frame"], "feedback world frame")
    links = value["links"]
    if not isinstance(links, dict) or not links:
        raise InvalidConfigError("feedback needs explicit link frames")
    for name, link in links.items():
        if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
            raise InvalidConfigError("feedback link names must be identifiers")
        if not isinstance(link, dict) or set(link) != {"frame", "rest"} or link["frame"] != f"{model}/{name}":
            raise InvalidConfigError(f"feedback link {name!r} must name its model-scoped frame")
        _pose_metadata(link["rest"], f"feedback link {name!r}")
    joints = value["joints"]
    if (
        not isinstance(joints, list)
        or not all(isinstance(name, str) and _IDENTIFIER.fullmatch(name) for name in joints)
        or len(set(joints)) != len(joints)
    ):
        raise InvalidConfigError("feedback joints must be unique physical joint identifiers")
    visuals = value["visuals"]
    if not isinstance(visuals, dict):
        raise InvalidConfigError("feedback visuals must be a map")
    for name, visual in visuals.items():
        if (
            not isinstance(name, str) or not _IDENTIFIER.fullmatch(name)
            or name in links or name in joints
        ):
            raise InvalidConfigError("driven visual names must be distinct identifiers")
        if not isinstance(visual, dict) or set(visual) != {"parent", "joint", "multiplier", "axis", "pose"}:
            raise InvalidConfigError(f"driven visual {name!r} has unsupported fields")
        if not isinstance(visual["parent"], str) or visual["parent"] not in links:
            raise InvalidConfigError(f"driven visual {name!r} needs a physical parent link")
        if not isinstance(visual["joint"], str) or visual["joint"] not in joints:
            raise InvalidConfigError(f"driven visual {name!r} needs a physical source joint")
        if not _finite(visual["multiplier"]):
            raise InvalidConfigError(f"driven visual {name!r} multiplier must be finite")
        axis = visual["axis"]
        if (
            not isinstance(axis, list) or len(axis) != 3 or not all(_finite(item) for item in axis)
            or not math.isclose(math.hypot(*axis), 1.0, abs_tol=1e-6)
        ):
            raise InvalidConfigError(f"driven visual {name!r} needs a unit spin axis")
        _pose_metadata(visual["pose"], f"driven visual {name!r}")


def _transmission_metadata(value: Any, model: str, feedback: dict, commands: dict[str, _Command]) -> None:
    if not isinstance(value, list):
        raise InvalidConfigError("transmissions must be a list")
    names: set[str] = set()
    outputs: set[str] = set()
    physical = set(feedback["joints"])
    for transmission in value:
        if not isinstance(transmission, dict) or set(transmission) != {"name", "joint", "ratio"}:
            raise InvalidConfigError("each transmission must declare name, joint and signed ratio")
        name, joint, ratio = (transmission[key] for key in ("name", "joint", "ratio"))
        if (
            not isinstance(name, str) or not _IDENTIFIER.fullmatch(name) or name in names
            or name in physical or name in feedback["links"] or name in feedback["visuals"]
            or name not in commands
        ):
            raise InvalidConfigError("transmission names must uniquely identify motor commands")
        if not isinstance(joint, str) or joint not in physical or joint in outputs or joint in commands:
            raise InvalidConfigError(f"transmission {name!r} needs a unique physical output joint")
        if not _finite(ratio) or ratio == 0:
            raise InvalidConfigError(f"transmission {name!r} ratio must be finite and nonzero")
        output_limit = abs(ratio) * commands[name].velocity_limit
        if not _finite(output_limit) or output_limit == 0:
            raise InvalidConfigError(f"transmission {name!r} has an unrepresentable output velocity limit")
        if commands[name].topic != f"/{model}/motors/{name}/cmd_vel":
            raise InvalidConfigError(f"transmission {name!r} must route to its motor input topic")
        names.add(name)
        outputs.add(joint)
    if not commands.keys() <= physical | names:
        raise InvalidConfigError("commands must identify physical joints or declared motor transmissions")


def _metadata(value: Any) -> tuple[str, dict[str, _Command]]:
    if not isinstance(value, dict) or set(value) != _CONFIG_FIELDS:
        raise InvalidConfigError(f"control metadata must contain exactly {', '.join(sorted(_CONFIG_FIELDS))}")
    if not isinstance(value["model"], str) or not _IDENTIFIER.fullmatch(value["model"]):
        raise InvalidConfigError("control model must be a valid identifier")
    for key in ("launch", "runtime"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise InvalidConfigError(f"control {key} must be a nonempty string")
    topics = set()
    for key in ("joint_states", "tf", "clock"):
        topic = _topic(value[key], f"control {key}")
        if topic in topics:
            raise InvalidConfigError(f"duplicate control topic {topic!r}")
        topics.add(topic)
    if not isinstance(value["commands"], list):
        raise InvalidConfigError("control commands must be a list")
    commands: dict[str, _Command] = {}
    for item in value["commands"]:
        if not isinstance(item, dict) or set(item) != _COMMAND_FIELDS:
            raise InvalidConfigError(f"each control command must contain exactly {', '.join(sorted(_COMMAND_FIELDS))}")
        joint = item["joint"]
        if not isinstance(joint, str) or not _IDENTIFIER.fullmatch(joint):
            raise InvalidConfigError("control joint must be a valid identifier")
        if joint in commands:
            raise InvalidConfigError(f"duplicate control joint {joint!r}")
        topic = _topic(item["topic"], f"joint {joint!r} topic")
        if topic in topics:
            raise InvalidConfigError(f"duplicate control topic {topic!r}")
        topics.add(topic)
        if item["type"] != "std_msgs/msg/Float64" or item["units"] != "rad/s":
            raise InvalidConfigError(f"joint {joint!r} requires std_msgs/msg/Float64 velocity commands in rad/s")
        for key in ("effort_limit", "velocity_limit"):
            if not _finite(item[key]) or item[key] <= 0:
                raise InvalidConfigError(f"joint {joint!r} {key} must be finite and positive")
        if not isinstance(item["example"], str) or not item["example"].strip():
            raise InvalidConfigError(f"joint {joint!r} example must be a nonempty string")
        commands[joint] = _Command(topic, float(item["velocity_limit"]))
    _feedback_metadata(value["feedback"], value["model"])
    _transmission_metadata(value["transmissions"], value["model"], value["feedback"], commands)
    try:
        metadata = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise InvalidConfigError(f"control metadata must be JSON: {error}") from error
    return metadata, commands


def _output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    parts = []
    for value in (stderr, stdout):
        if isinstance(value, bytes):
            value = value.decode(errors="replace")
        if value and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def _publish(prefix: tuple[str, ...], command: _Command, value: float) -> str | None:
    """Return a transport failure, not a motor-response claim. Inputs are validated."""
    try:
        done = subprocess.run(
            [
                *prefix, "topic", "pub", "--once", "--max-wait-time-secs", "3",
                "--wait-matching-subscriptions", "1", "--keep-alive", "0.1",
                command.topic, "std_msgs/msg/Float64", json.dumps({"data": value}, allow_nan=False),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_PUBLISH_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        detail = _output(error.stdout, error.stderr)
        return f"ROS publisher timed out after {_PUBLISH_TIMEOUT} s" + (f": {detail}" if detail else "")
    except OSError as error:
        return f"could not start ROS publisher: {error}"
    if done.returncode:
        detail = _output(done.stdout, done.stderr)
        return f"ROS publisher exited with status {done.returncode}" + (f": {detail}" if detail else "")
    return None


class Controls:
    """Project-scoped control snapshots and ordered, bounded one-shot publishing."""

    def __init__(
        self, load: Callable[[dict[str, str]], dict[str, Any]], *, ros_command: tuple[str, ...] = ("ros2",)
    ):
        if not ros_command or any(not isinstance(arg, str) or not arg for arg in ros_command):
            raise ValueError("ros_command must contain an executable and nonempty arguments")
        self._ros_command = ros_command
        self._load = load
        self._namespace = secrets.token_bytes(32)
        self._targets: dict[tuple[str, str], _Target] = {}
        self._lock = threading.Lock()

    def _target(self, target: dict[str, str]) -> tuple[dict[str, str], _Target]:
        if (
            not isinstance(target, dict)
            or set(target) != {"kind", "name"}
            or target["kind"] != "assembly"
            or not isinstance(target["name"], str)
            or not target["name"].strip()
        ):
            raise ValueError("expected an assembly target with kind and nonempty name")
        canonical = {"kind": "assembly", "name": target["name"]}
        key = ("assembly", target["name"])
        with self._lock:
            state = self._targets.get(key)
            if state is None:
                state = self._targets[key] = _Target()
        return canonical, state

    def describe(self, target: dict[str, str]) -> dict[str, Any]:
        canonical, state = self._target(target)
        with state.serialized():
            # A failed refresh must not leave old routing or limits usable.
            state.snapshot = None
            metadata, commands = _metadata(self._load(dict(canonical)))
            identity = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
            config_id = hashlib.sha256(self._namespace + b"\0" + identity + b"\0" + metadata.encode()).hexdigest()
            state.snapshot = _Snapshot(config_id, metadata, commands)
            return {**json.loads(metadata), "config_id": config_id, "target": canonical}

    def snapshot(self, target: dict[str, str], config_id: str) -> dict[str, Any]:
        """Read the exact approved metadata without rebuilding its factory or importing ROS."""
        canonical, state = self._target(target)
        with state.serialized():
            snapshot = state.snapshot
            if snapshot is None or config_id != snapshot.config_id:
                raise ConfigConflictError("simulation controls changed; reload them before connecting feedback")
            return {**json.loads(snapshot.metadata), "config_id": snapshot.config_id, "target": canonical}

    def send(
        self,
        target: dict[str, str],
        config_id: str,
        values: dict[str, float] | None = None,
        *,
        stop_all: bool = False,
    ) -> dict[str, dict[str, Any]]:
        _, state = self._target(target)
        with state.serialized():
            snapshot = state.snapshot
            if snapshot is None:
                raise ConfigConflictError("load this target's simulation controls before sending commands")
            if not isinstance(config_id, str) or config_id != snapshot.config_id:
                raise ConfigConflictError("simulation controls changed; refresh them before sending commands")
            if not isinstance(stop_all, bool):
                raise ValueError("stop_all must be a boolean")
            if stop_all:
                if values is not None:
                    raise ValueError("stop_all cannot be combined with values")
                requested = dict.fromkeys(snapshot.commands, 0.0)
            else:
                if not isinstance(values, dict) or not values:
                    raise ValueError("provide a nonempty values object or stop_all: true")
                requested: dict[str, float] = {}
                for joint, value in values.items():
                    if not isinstance(joint, str):
                        raise ValueError("joint names must be strings")
                    command = snapshot.commands.get(joint)
                    if command is None:
                        raise LookupError(f"unknown control joint {joint!r}")
                    if not _finite(value) or not -command.velocity_limit <= value <= command.velocity_limit:
                        raise ValueError(
                            f"joint {joint!r} velocity must be finite and within "
                            f"[-{command.velocity_limit:g}, {command.velocity_limit:g}] rad/s"
                        )
                    requested[joint] = float(value)
            published: dict[str, float] = {}
            failed: dict[str, str] = {}
            if not requested:
                return {"published": published, "failed": failed}
            executable = shutil.which(self._ros_command[0])
            if executable is None:
                raise RuntimeError(
                    f"ROS command {self._ros_command[0]!r} is unavailable on the server's PATH: "
                    "source ROS 2 before starting Cadiste or configure --ros-command for an existing runtime"
                )
            prefix = (executable, *self._ros_command[1:])
            with ThreadPoolExecutor(max_workers=min(_MAX_PUBLISHERS, len(requested))) as pool:
                futures = {
                    joint: pool.submit(_publish, prefix, snapshot.commands[joint], value)
                    for joint, value in requested.items()
                }
                for joint, future in futures.items():
                    reason = future.result()
                    if reason is None:
                        published[joint] = requested[joint]
                    else:
                        failed[joint] = reason
            return {"published": published, "failed": failed}
