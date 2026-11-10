from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from typing import Any

import pytest

import cadiste.ros as ros
from cadiste.ros import ConfigConflictError, Controls, InvalidConfigError

_TARGET = {"kind": "assembly", "name": "Inspection robot"}


def _metadata(count: int = 2) -> dict[str, Any]:
    return {
        "model": "inspection_bot",
        "commands": [
            {
                "joint": f"motor_{index}",
                "topic": f"/inspection_bot/joints/motor_{index}/cmd_vel",
                "type": "std_msgs/msg/Float64",
                "units": "rad/s",
                "effort_limit": 2.5,
                "velocity_limit": 4.0,
                "example": "An adapter's display-only example",
            }
            for index in range(count)
        ],
        "joint_states": "/inspection_bot/joint_states",
        "tf": "/tf",
        "clock": "/clock",
        "launch": "An adapter's display-only launch instruction",
        "runtime": "ROS 2 Jazzy / Gazebo Harmonic",
        "transmissions": [],
        "feedback": {
            "world_frame": "cadiste",
            "model_frame": "inspection_bot",
            "links": {
                "body": {
                    "frame": "inspection_bot/body",
                    "rest": {"position": [0, 0, 0], "quaternion": [0, 0, 0, 1]},
                },
            },
            "joints": [f"motor_{index}" for index in range(count)],
            "visuals": {},
        },
    }


def _geared_metadata() -> dict[str, Any]:
    metadata = _metadata(1)
    metadata["commands"][0].update(
        joint="drive_motor", topic="/inspection_bot/motors/drive_motor/cmd_vel",
    )
    metadata["transmissions"] = [{"name": "drive_motor", "joint": "axle", "ratio": -0.4}]
    metadata["feedback"]["joints"] = ["axle"]
    metadata["feedback"]["visuals"] = {
        "pinion": {
            "parent": "body", "joint": "axle", "multiplier": -2.5, "axis": [0, 1, 0],
            "pose": {"position": [0.02, 0, 0], "quaternion": [0, 0, 0, 1]},
        },
    }
    return metadata


@pytest.fixture
def forbidden_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("rejected input reached the ROS transport boundary")

    monkeypatch.setattr(ros.shutil, "which", forbidden)
    monkeypatch.setattr(ros, "_publish", forbidden)


def _background(action: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["value"] = action()
        except Exception as error:
            result["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def _finish(job: tuple[threading.Thread, dict[str, Any]]) -> Any:
    thread, result = job
    thread.join(3)
    assert not thread.is_alive(), "control request did not finish"
    if "error" in result:
        raise result["error"]
    return result["value"]


@pytest.mark.parametrize(("field", "value"), [
    ("model", "../other"),
    ("model", 1),
    ("commands", {}),
    ("commands", [None]),
    ("joint_states", "/bad;touch /tmp/injected"),
    ("tf", "--help"),
    ("clock", "/tf"),
    ("launch", None),
    ("runtime", ""),
])
def test_invalid_top_level_metadata_cannot_authorize_commands(field: str, value: Any, forbidden_transport: None):
    metadata = _metadata()
    metadata[field] = value
    controls = Controls(lambda target: metadata)
    with pytest.raises(InvalidConfigError):
        controls.describe(_TARGET)
    with pytest.raises(ConfigConflictError, match="load"):
        controls.send(_TARGET, "anything", {"motor_0": 1.0})


@pytest.mark.parametrize(("field", "value"), [
    ("joint", "--help"),
    ("joint", []),
    ("type", "std_msgs/msg/String"),
    ("type", "--help"),
    ("units", "m/s"),
    ("effort_limit", -1),
    ("effort_limit", float("nan")),
    ("velocity_limit", 0),
    ("velocity_limit", -1),
    ("velocity_limit", float("nan")),
    ("velocity_limit", float("inf")),
    ("velocity_limit", float("-inf")),
    ("velocity_limit", True),
    ("velocity_limit", "4"),
    ("velocity_limit", 10**400),
    ("example", []),
])
def test_invalid_command_schema_is_rejected(field: str, value: Any, forbidden_transport: None):
    metadata = _metadata()
    metadata["commands"][0][field] = value
    controls = Controls(lambda target: metadata)
    with pytest.raises(InvalidConfigError):
        controls.describe(_TARGET)


@pytest.mark.parametrize("topic", [
    "--help", "relative/topic", "/", "/trailing/", "/two//slashes", "/repeated__underscores",
    "/starts/1digit", "/bad-name", "/bad name", "/bad\nname", "/bad\x00name", "/$(touch injected)",
    "/name;touch injected", "/{substitution}", "/~private", "/" + "x" * 255,
])
def test_unsafe_or_invalid_ros_names_never_reach_a_process(topic: str, forbidden_transport: None):
    metadata = _metadata()
    metadata["commands"][0]["topic"] = topic
    controls = Controls(lambda target: metadata)
    with pytest.raises(InvalidConfigError, match="absolute ROS topic"):
        controls.describe(_TARGET)


@pytest.mark.parametrize("field", ["joint", "topic"])
def test_duplicate_command_routes_are_rejected(field: str, forbidden_transport: None):
    metadata = _metadata()
    metadata["commands"][1][field] = metadata["commands"][0][field]
    controls = Controls(lambda target: metadata)
    with pytest.raises(InvalidConfigError, match="duplicate"):
        controls.describe(_TARGET)


def test_command_topic_cannot_collide_with_state_topic(forbidden_transport: None):
    metadata = _metadata()
    metadata["commands"][0]["topic"] = metadata["joint_states"]
    with pytest.raises(InvalidConfigError, match="duplicate"):
        Controls(lambda target: metadata).describe(_TARGET)


@pytest.mark.parametrize("change", [
    lambda metadata: metadata.pop("model"),
    lambda metadata: metadata.update(executable="/bin/sh"),
    lambda metadata: metadata["commands"][0].pop("velocity_limit"),
    lambda metadata: metadata["commands"][0].update(shell=True),
])
def test_missing_fields_and_executable_configuration_are_not_supported(change, forbidden_transport: None):
    metadata = _metadata()
    change(metadata)
    with pytest.raises(InvalidConfigError, match="exactly"):
        Controls(lambda target: metadata).describe(_TARGET)


def test_unchanged_metadata_has_stable_identity_but_changed_limits_invalidate_old_requests(forbidden_transport: None):
    metadata = _metadata()
    controls = Controls(lambda target: metadata)
    first = controls.describe(_TARGET)
    metadata = {key: value for key, value in reversed(list(metadata.items()))}
    metadata["commands"] = [dict(reversed(list(command.items()))) for command in metadata["commands"]]
    assert controls.describe(dict(reversed(list(_TARGET.items()))))["config_id"] == first["config_id"]

    metadata["commands"][0]["velocity_limit"] = 0.5
    second = controls.describe(_TARGET)
    assert second["config_id"] != first["config_id"]
    with pytest.raises(ConfigConflictError, match="refresh"):
        controls.send(_TARGET, first["config_id"], {"motor_0": 2})
    with pytest.raises(ValueError, match="within"):
        controls.send(_TARGET, second["config_id"], {"motor_0": 2})


def test_mutating_descriptions_or_adapter_objects_cannot_change_an_authorized_limit(forbidden_transport: None):
    metadata = _metadata()
    controls = Controls(lambda target: metadata)
    description = controls.describe(_TARGET)
    description["commands"][0]["velocity_limit"] = 1000
    metadata["commands"][0]["velocity_limit"] = 1000
    metadata["commands"][0]["topic"] = "/other/robot"
    with pytest.raises(ValueError, match="within"):
        controls.send(_TARGET, description["config_id"], {"motor_0": 50})


def test_configuration_ids_are_bound_to_target_and_project(forbidden_transport: None):
    metadata = _metadata()
    controls = Controls(lambda target: metadata)
    first = controls.describe(_TARGET)
    other_target = {"kind": "assembly", "name": "Different assembly"}
    second = controls.describe(other_target)
    assert first["config_id"] != second["config_id"]
    with pytest.raises(ConfigConflictError):
        controls.send(other_target, first["config_id"], {"motor_0": 1})

    other_project = Controls(lambda target: metadata)
    third = other_project.describe(_TARGET)
    assert first["config_id"] != third["config_id"]
    with pytest.raises(ConfigConflictError):
        other_project.send(_TARGET, first["config_id"], stop_all=True)


@pytest.mark.parametrize("failure", [RuntimeError("adapter failed"), ValueError("adapter rejected input")])
def test_failed_refresh_revokes_previous_configuration(failure: Exception, forbidden_transport: None):
    def load(target: dict[str, str]) -> dict[str, Any]:
        if broken:
            raise failure
        return _metadata()

    broken = False
    controls = Controls(load)
    description = controls.describe(_TARGET)
    broken = True
    with pytest.raises(type(failure), match=str(failure)):
        controls.describe(_TARGET)
    with pytest.raises(ConfigConflictError, match="load"):
        controls.send(_TARGET, description["config_id"], stop_all=True)
    broken = False
    assert controls.describe(_TARGET)["config_id"] == description["config_id"]


@pytest.mark.parametrize("target", [
    {}, {"kind": "part", "name": "motor"}, {"kind": "assembly", "name": ""},
    {"kind": "assembly", "name": []}, {**_TARGET, "topic": "/injected"},
])
def test_noncanonical_targets_are_rejected(target: Any, forbidden_transport: None):
    controls = Controls(lambda target: _metadata())
    with pytest.raises(ValueError, match="assembly target"):
        controls.describe(target)
    with pytest.raises(ValueError, match="assembly target"):
        controls.send(target, "anything", {"motor_0": 0})


def test_malformed_refresh_revokes_previously_valid_routes(forbidden_transport: None):
    metadata = _metadata()
    controls = Controls(lambda target: metadata)
    first = controls.describe(_TARGET)
    metadata["commands"][0]["topic"] = "--help"
    with pytest.raises(InvalidConfigError, match="absolute ROS topic"):
        controls.describe(_TARGET)
    with pytest.raises(ConfigConflictError, match="load"):
        controls.send(_TARGET, first["config_id"], stop_all=True)


def test_sending_does_not_implicitly_load_an_unknown_target(forbidden_transport: None):
    def load(target: dict[str, str]) -> dict[str, Any]:
        pytest.fail("send attempted to load an unapproved configuration")

    with pytest.raises(ConfigConflictError, match="load"):
        Controls(load).send(_TARGET, "anything", {"motor_0": 1})


@pytest.mark.parametrize("value", [
    4.00001, -4.00001, float("nan"), float("inf"), float("-inf"), True, False,
    "1", None, [], {}, 10**400,
])
def test_batch_validation_is_atomic_even_after_a_valid_joint(value: Any, forbidden_transport: None):
    controls = Controls(lambda target: _metadata())
    description = controls.describe(_TARGET)
    with pytest.raises(ValueError, match="finite and within"):
        controls.send(_TARGET, description["config_id"], {"motor_0": 1, "motor_1": value})


@pytest.mark.parametrize("joint", ["absent", "/injected/topic", "--help", "$(touch injected)"])
def test_unknown_joint_cannot_route_an_arbitrary_topic(joint: str, forbidden_transport: None):
    controls = Controls(lambda target: _metadata())
    description = controls.describe(_TARGET)
    with pytest.raises(LookupError, match="unknown control joint"):
        controls.send(_TARGET, description["config_id"], {"motor_0": 1, joint: 0})


@pytest.mark.parametrize(("values", "stop_all"), [
    (None, False), ({}, False), ([], False), ({"motor_0": 1}, True), ({}, True),
    ({"motor_0": 1}, 1), ({"motor_0": 1}, "true"), ({1: 1}, False),
])
def test_malformed_send_modes_are_rejected(values: Any, stop_all: Any, forbidden_transport: None):
    controls = Controls(lambda target: _metadata())
    description = controls.describe(_TARGET)
    with pytest.raises(ValueError):
        controls.send(_TARGET, description["config_id"], values, stop_all=stop_all)


@pytest.mark.parametrize("value", [-4, -0.0, 0, 4])
def test_signed_boundary_values_are_accepted_but_missing_ros_is_explicit(value: float, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", "")
    controls = Controls(lambda target: _metadata())
    description = controls.describe(_TARGET)
    with pytest.raises(RuntimeError, match="server's PATH"):
        controls.send(_TARGET, description["config_id"], {"motor_0": value})
    with pytest.raises(RuntimeError, match="server's PATH"):
        controls.send(_TARGET, description["config_id"], stop_all=True)


def test_unactuated_model_can_be_described_and_has_nothing_to_stop(forbidden_transport: None):
    controls = Controls(lambda target: _metadata(0))
    description = controls.describe(_TARGET)
    assert description["commands"] == []
    assert controls.send(_TARGET, description["config_id"], stop_all=True) == {"published": {}, "failed": {}}


def test_stop_all_attempts_every_joint_despite_partial_transport_failure(monkeypatch: pytest.MonkeyPatch):
    metadata = _metadata(3)
    controls = Controls(lambda target: metadata)
    description = controls.describe(_TARGET)
    monkeypatch.setattr(ros.shutil, "which", lambda name: "/unused/ros2")
    attempted: set[str] = set()
    lock = threading.Lock()

    def publish(executable: str, command: Any, value: float) -> str | None:
        with lock:
            attempted.add(command.topic)
        return "subscription disappeared" if command.topic.endswith("/motor_0/cmd_vel") else None

    monkeypatch.setattr(ros, "_publish", publish)
    result = controls.send(_TARGET, description["config_id"], stop_all=True)
    assert attempted == {command["topic"] for command in metadata["commands"]}
    assert set(result["failed"]) == {"motor_0"}
    assert result["published"] == {"motor_1": 0.0, "motor_2": 0.0}


def test_stop_waits_for_all_older_publishers_and_cannot_be_overwritten(monkeypatch: pytest.MonkeyPatch):
    controls = Controls(lambda target: _metadata())
    description = controls.describe(_TARGET)
    monkeypatch.setattr(ros.shutil, "which", lambda name: "/unused/ros2")
    release = threading.Event()
    both_started = threading.Event()
    stop_started = threading.Event()
    stop_published = threading.Event()
    lock = threading.Lock()
    history: dict[str, list[float]] = {}
    running = 0

    def publish(executable: str, command: Any, value: float) -> None:
        nonlocal running
        if value:
            with lock:
                running += 1
                if running == 2:
                    both_started.set()
            assert release.wait(3), "older send was never released"
        else:
            stop_published.set()
        with lock:
            history.setdefault(command.topic, []).append(value)

    def stop() -> dict[str, Any]:
        stop_started.set()
        return controls.send(_TARGET, description["config_id"], stop_all=True)

    monkeypatch.setattr(ros, "_publish", publish)
    sending = _background(lambda: controls.send(_TARGET, description["config_id"], {"motor_0": 2, "motor_1": -3}))
    stopping = None
    try:
        assert both_started.wait(2)
        stopping = _background(stop)
        assert stop_started.wait(2)
        assert not stop_published.wait(0.05), "stop overtook publishers from an older send"
    finally:
        release.set()
    assert _finish(sending)["failed"] == {}
    assert stopping is not None
    assert _finish(stopping)["failed"] == {}
    assert sorted(history.values()) == [[-3.0, 0.0], [2.0, 0.0]]


def test_refresh_serializes_with_send_and_rejects_the_old_snapshot(forbidden_transport: None):
    refreshing = threading.Event()
    release = threading.Event()
    metadata = _metadata()
    replacement = copy.deepcopy(metadata)
    replacement["commands"][0]["velocity_limit"] = 0.5
    refresh = False

    def load(target: dict[str, str]) -> dict[str, Any]:
        if refresh:
            refreshing.set()
            assert release.wait(3), "refresh was never released"
            return replacement
        return metadata

    controls = Controls(load)
    first = controls.describe(_TARGET)
    refresh = True
    describing = _background(lambda: controls.describe(_TARGET))
    sending = None
    try:
        assert refreshing.wait(2)
        sending = _background(lambda: controls.send(_TARGET, first["config_id"], {"motor_0": 2}))
    finally:
        release.set()
    assert _finish(describing)["config_id"] != first["config_id"]
    assert sending is not None
    with pytest.raises(ConfigConflictError, match="refresh"):
        _finish(sending)


@pytest.mark.parametrize(("field", "value"), [
    ("ratio", 0), ("ratio", False), ("ratio", float("nan")), ("ratio", float("inf")),
    ("name", "axle"), ("name", "body"), ("name", "pinion"), ("name", "unknown"),
    ("joint", "missing"), ("joint", []),
])
def test_invalid_transmission_cannot_authorize_motor_commands(field, value, forbidden_transport):
    metadata = _geared_metadata()
    metadata["transmissions"][0][field] = value
    controls = Controls(lambda target: metadata)
    with pytest.raises(InvalidConfigError):
        controls.describe(_TARGET)
    with pytest.raises(ConfigConflictError):
        controls.send(_TARGET, "anything", {"drive_motor": 1.0})


@pytest.mark.parametrize(("field", "value"), [
    ("parent", "missing"), ("parent", []), ("joint", "drive_motor"),
    ("axis", [0, 0, 0]), ("axis", [0, 2, 0]), ("axis", [0, float("nan"), 0]),
    ("multiplier", float("inf")),
    ("pose", {"position": [0, 0, 0], "quaternion": [0, 0, 0, 0]}),
])
def test_invalid_gear_dependencies_cannot_authorize_motor_commands(field, value, forbidden_transport):
    metadata = _geared_metadata()
    metadata["feedback"]["visuals"]["pinion"][field] = value
    with pytest.raises(InvalidConfigError):
        Controls(lambda target: metadata).describe(_TARGET)


def test_transmission_direction_change_invalidates_previous_command_and_feedback_snapshot(forbidden_transport):
    metadata = _geared_metadata()
    controls = Controls(lambda target: metadata)
    previous = controls.describe(_TARGET)
    metadata["transmissions"][0]["ratio"] = 0.4
    current = controls.describe(_TARGET)
    with pytest.raises(ConfigConflictError):
        controls.send(_TARGET, previous["config_id"], {"drive_motor": 1.0})
    with pytest.raises(ConfigConflictError):
        controls.snapshot(_TARGET, previous["config_id"])
    assert controls.snapshot(_TARGET, current["config_id"])["transmissions"][0]["ratio"] == 0.4
