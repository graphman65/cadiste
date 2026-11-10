from __future__ import annotations

import json
import math
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cadiste.feedback import Feedback

_WORKER = Path(__file__).resolve().parents[1] / "ros/cadiste_ros/lib/cadiste_ros/feedback"
Measurements = runpy.run_path(str(_WORKER))["Measurements"]
_IDENTITY = (0.0, 0.0, 0.0, 1.0)


def _config() -> dict[str, Any]:
    return {
        "model": "crawler", "joint_states": "/crawler/joint_states", "tf": "/tf", "clock": "/clock",
        "transmissions": [],
        "feedback": {
            "world_frame": "cadiste", "model_frame": "crawler",
            "links": {"wheel": {"frame": "crawler/wheel"}, "base": {"frame": "crawler/base"}},
            "joints": ["motor", "steer"],
            "visuals": {},
        },
    }


@pytest.fixture
def state():
    wall = [100.0]
    measured = Measurements(_config(), monotonic=lambda: wall[0])
    return measured, wall


def _populate(state, stamp=12.0):
    state.update_clock(stamp)
    # Deliberately child-first: this must be a graph, not arrival-order FK.
    state.update_transform("crawler/base", "crawler/wheel", (0.2, 0, 0), _IDENTITY, stamp)
    state.update_transform("crawler", "crawler/base", (0, 0, 0.1), _IDENTITY, stamp)
    state.update_transform("cadiste", "crawler", (-0.57, 0.1, 0.4),
                           (0, 0, math.sin(math.pi / 4), math.cos(math.pi / 4)), stamp)
    state.update_joints(["motor", "steer"], [1.25, -0.1], [3.5, -0.25], [], stamp)


def test_measured_world_pose_composes_nonzero_rotated_base_and_nested_links(state):
    measured, _ = state
    _populate(measured)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "live"
    assert snapshot["stamp"] == 12.0
    assert snapshot["age"] == 0.0
    assert snapshot["missing_frames"] == []
    assert snapshot["model_pose"]["position"] == pytest.approx([-0.57, 0.1, 0.4])
    assert snapshot["links"]["wheel"]["position"] == pytest.approx([-0.57, 0.3, 0.5])
    assert snapshot["links"]["wheel"]["quaternion"] == pytest.approx([0, 0, 2**-0.5, 2**-0.5])
    assert snapshot["joints"]["motor"] == {"position": 1.25, "velocity": 3.5, "effort": None}


def test_rotations_compose_in_parent_order_not_componentwise_addition(state):
    measured, _ = state
    _populate(measured)
    measured.update_transform("crawler/base", "crawler/wheel", (0.2, 0, 0),
                              (2**-0.5, 0, 0, 2**-0.5), 12.1)
    wheel = measured.snapshot()["links"]["wheel"]
    assert wheel["quaternion"] == pytest.approx([0.5, 0.5, 0.5, 0.5])
    assert wheel["position"] == pytest.approx([-0.57, 0.3, 0.5])


def test_newer_transforms_survive_out_of_order_updates_and_normalize_quaternions(state):
    measured, _ = state
    _populate(measured)
    measured.update_transform("crawler/base", "crawler/wheel", (0.3, 0, 0), (0, 0, 0, 3), 13)
    measured.update_transform("crawler/base", "crawler/wheel", (8, 9, 10), _IDENTITY, 11)
    measured.update_joints(["motor"], [4], [5], [6], 13)
    measured.update_joints(["motor"], [90], [91], [92], 11)
    snapshot = measured.snapshot()
    assert snapshot["links"]["wheel"]["position"] == pytest.approx([-0.57, 0.4, 0.5])
    assert snapshot["joints"]["motor"] == {"position": 4, "velocity": 5, "effort": 6}


def test_waiting_exposes_missing_frames_and_never_uses_rest_or_zero_joint_values(state):
    measured, _ = state
    measured.update_transform("missing_parent", "crawler/wheel", (1, 0, 0), _IDENTITY, 1)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "waiting"
    assert snapshot["model_pose"] is None
    assert snapshot["links"] == {}
    assert "missing_parent" in snapshot["missing_frames"]
    assert "crawler" in snapshot["missing_frames"]
    assert snapshot["joints"]["motor"] == {"position": None, "velocity": None, "effort": None}


def test_tf_cycles_are_missing_not_identity_or_recursive_crashes(state):
    measured, _ = state
    measured.update_transform("crawler/wheel", "crawler/base", (0, 0, 0), _IDENTITY, 1)
    measured.update_transform("crawler/base", "crawler/wheel", (0, 0, 0), _IDENTITY, 1)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "waiting"
    assert {"crawler/base", "crawler/wheel"} <= set(snapshot["missing_frames"])
    assert snapshot["links"] == {}


def test_freshness_uses_oldest_required_joint_or_tf_not_latest_message(state):
    measured, wall = state
    _populate(measured)
    old = measured.snapshot()
    wall[0] += 1.1
    measured.update_joints(["motor", "steer"], [2, 3], [4, 5], [6, 7], 13.1)
    stale = measured.snapshot()
    assert stale["status"] == "stale"
    assert stale["age"] == pytest.approx(1.1)
    assert stale["links"] == old["links"]
    # All other data being new cannot freshen this one old parent transform.
    measured.update_transform("crawler/base", "crawler/wheel", (0.2, 0, 0), _IDENTITY, 13.1)
    measured.update_transform("crawler", "crawler/base", (0, 0, 0.1), _IDENTITY, 13.1)
    assert measured.snapshot()["status"] == "stale"
    measured.update_transform("cadiste", "crawler", (1, 2, 3), _IDENTITY, 13.1)
    assert measured.snapshot()["status"] == "live"


@pytest.mark.parametrize("position,rotation", [
    ((float("nan"), 0, 0), _IDENTITY),
    ((float("inf"), 0, 0), _IDENTITY),
    ((0, 0, 0), (0, 0, 0, 0)),
    ((0, 0, 0), (0, 0, 0, float("nan"))),
    ((0, 0), _IDENTITY),
])
def test_invalid_transform_retains_last_measurement_but_cannot_be_live(state, position, rotation):
    measured, wall = state
    _populate(measured)
    old = measured.snapshot()
    wall[0] += 2
    measured.update_transform("crawler/base", "crawler/wheel", position, rotation, 14)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "stale"
    assert snapshot["links"]["wheel"] == old["links"]["wheel"]
    assert snapshot["age"] == 2
    assert "crawler/wheel" in snapshot["missing_frames"]
    json.dumps(snapshot, allow_nan=False)


def test_joint_arrays_are_indexed_by_name_partial_messages_aggregate_and_effort_is_optional(state):
    measured, _ = state
    _populate(measured)
    measured.update_joints(["unrelated", "steer"], [99, -2], [100, -3], [101, -4], 13)
    measured.update_joints(["motor"], [4], [5], [], 13)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "live"
    assert snapshot["joints"] == {
        "motor": {"position": 4, "velocity": 5, "effort": None},
        "steer": {"position": -2, "velocity": -3, "effort": -4},
    }


def test_absent_nonfinite_and_duplicate_joint_values_cannot_claim_live(state):
    measured, _ = state
    _populate(measured)
    assert measured.snapshot()["status"] == "live"
    measured.update_joints(["motor", "steer"], [float("nan"), 4], [float("inf")], [], 13)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "stale"
    assert snapshot["joints"]["motor"] == {"position": None, "velocity": None, "effort": None}
    assert snapshot["joints"]["steer"]["velocity"] is None
    json.dumps(snapshot, allow_nan=False)
    measured.update_joints(["motor", "motor"], [1, 2], [3, 4], [], 14)
    assert measured.snapshot()["joints"]["motor"]["position"] is None


def test_incomplete_first_joint_message_waits_even_with_all_frames_present(state):
    measured, _ = state
    _populate(measured)
    measured.update_joints(["motor", "steer"], [1, 2], [], [], 13)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "waiting"
    assert snapshot["missing_frames"] == []
    assert snapshot["joints"]["motor"]["position"] == 1
    assert snapshot["joints"]["motor"]["velocity"] is None


def test_overflow_in_composed_geometry_is_missing_not_nonfinite_json(state):
    measured, _ = state
    measured.update_transform("cadiste", "crawler", (1e308, 0, 0), _IDENTITY, 1)
    measured.update_transform("crawler", "crawler/base", (1e308, 0, 0), _IDENTITY, 1)
    measured.update_transform("crawler/base", "crawler/wheel", (0, 0, 0), _IDENTITY, 1)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "waiting"
    assert "crawler/base" in snapshot["missing_frames"]
    assert snapshot["links"] == {}
    json.dumps(snapshot, allow_nan=False)


def test_clock_reset_clears_previous_epoch_and_accepts_new_lower_stamps(state):
    measured, _ = state
    _populate(measured, 42)
    assert measured.snapshot()["status"] == "live"
    measured.update_clock(0.0)
    reset = measured.snapshot()
    assert reset["status"] == "waiting"
    assert reset["stamp"] is None
    assert reset["age"] is None
    assert reset["model_pose"] is None
    assert reset["links"] == {}
    assert reset["joints"]["motor"]["position"] is None
    _populate(measured, 0.1)
    assert measured.snapshot()["status"] == "live"
    assert measured.snapshot()["stamp"] == 0.1


def test_clock_pause_is_not_a_reset_and_wall_freshness_still_expires(state):
    measured, wall = state
    _populate(measured)
    wall[0] += 2
    measured.update_clock(12)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "stale"
    assert snapshot["stamp"] == 12
    assert snapshot["age"] == 2


@pytest.fixture
def derived_state():
    config = _config()
    config["transmissions"] = [
        {"name": "drive_motor", "joint": "motor", "ratio": -0.4},
        {"name": "steering_motor", "joint": "steer", "ratio": 1.0},
    ]
    config["feedback"]["visuals"] = {
        "gear": {
            "parent": "base", "joint": "motor", "multiplier": -2.0,
            "axis": [0, 1, 0],
            "pose": {"position": [0.1, 0.2, 0.3], "quaternion": [2**-0.5, 0, 0, 2**-0.5]},
        },
    }
    wall = [100.0]
    return Measurements(config, monotonic=lambda: wall[0]), wall


@pytest.mark.parametrize("ratio", [0.4, -0.4, 1.0, -1.0])
def test_virtual_motor_coordinates_and_effort_preserve_signed_measured_power(ratio):
    config = _config()
    config["transmissions"] = [{"name": "input", "joint": "motor", "ratio": ratio}]
    measured = Measurements(config, monotonic=lambda: 100.0)
    _populate(measured)
    measured.update_joints(["motor"], [1.2], [-3.0], [7.5], 13)
    snapshot = measured.snapshot()
    motor = snapshot["joints"]["input"]
    assert snapshot["status"] == "live"
    assert motor == pytest.approx({"position": 1.2 / ratio, "velocity": -3.0 / ratio, "effort": 7.5 * ratio})
    assert motor["effort"] * motor["velocity"] == pytest.approx(7.5 * -3.0)
    assert snapshot["joints"]["motor"] == {"position": 1.2, "velocity": -3.0, "effort": 7.5}


def test_visual_pose_rotates_in_pivot_axes_and_tracks_measured_moving_parent(derived_state):
    measured, _ = derived_state
    _populate(measured)
    measured.update_joints(["motor"], [math.pi / 4], [0], [], 13)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "live"
    assert snapshot["links"]["gear"]["position"] == pytest.approx([-0.77, 0.2, 0.8])
    assert snapshot["links"]["gear"]["quaternion"] == pytest.approx([2**-0.5, 0, 0, 2**-0.5])
    assert snapshot["joints"]["drive_motor"]["position"] == pytest.approx(-math.pi / 1.6)
    assert snapshot["joints"]["drive_motor"]["effort"] is None
    measured.update_transform("cadiste", "crawler", (3, 4, 5), _IDENTITY, 13)
    moved = measured.snapshot()
    assert moved["links"]["gear"]["position"] == pytest.approx([3.1, 4.2, 5.4])
    assert moved["links"]["gear"]["quaternion"] == pytest.approx([0.5, -0.5, -0.5, 0.5])
    assert moved["joints"]["drive_motor"] == snapshot["joints"]["drive_motor"]


def test_derived_partial_sources_and_virtual_name_messages_cannot_invent_measurements(derived_state):
    measured, _ = derived_state
    measured.update_joints(["drive_motor"], [800], [900], [1000], 12)
    waiting = measured.snapshot()
    assert waiting["status"] == "waiting"
    assert waiting["joints"]["drive_motor"] == dict.fromkeys(("position", "velocity", "effort"))
    assert "gear" not in waiting["links"]
    measured.update_joints(["motor"], [0.25], [], [], 12)
    assert measured.snapshot()["joints"]["drive_motor"] == {"position": -0.625, "velocity": None, "effort": None}
    assert "gear" not in measured.snapshot()["links"]
    _populate(measured)
    measured.update_joints(["unrelated", "motor"], [999, 2], [999, -4], [], 13)
    measured.update_joints(["motor"], [100], [100], [100], 11)
    measured.update_transform("cadiste", "crawler", (math.nan, 0, 0), _IDENTITY, 11)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "live"
    assert snapshot["joints"]["drive_motor"] == {"position": -5.0, "velocity": 10.0, "effort": None}
    assert snapshot["joints"]["steering_motor"] == snapshot["joints"]["steer"]
    assert snapshot["links"]["gear"]["position"] == pytest.approx([-0.77, 0.2, 0.8])


def test_visual_requires_actual_joint_position_and_parent_not_rest_pose(derived_state):
    measured, _ = derived_state
    _populate(measured)
    measured.update_joints(["motor"], [], [4], [], 13)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "waiting"
    assert "gear" not in snapshot["links"]
    assert snapshot["joints"]["drive_motor"] == {"position": None, "velocity": -10.0, "effort": None}
    measured.update_joints(["motor"], [0], [0], [], 14)
    at_zero = measured.snapshot()
    assert at_zero["status"] == "live"
    assert at_zero["links"]["gear"]["quaternion"] == pytest.approx([0.5, 0.5, 0.5, 0.5])


def test_stale_visual_retains_pose_until_both_sources_are_fresh(derived_state):
    measured, wall = derived_state
    _populate(measured)
    initial = measured.snapshot()
    wall[0] += 1.1
    measured.update_joints(["motor", "steer"], [9, 8], [7, 6], [], 14)
    stale = measured.snapshot()
    assert stale["status"] == "stale"
    assert stale["age"] == pytest.approx(1.1)
    assert stale["links"]["gear"] == initial["links"]["gear"]
    measured.update_transform("crawler", "crawler/base", (0, 0, 0.1), _IDENTITY, 14)
    measured.update_transform("cadiste", "crawler", (3, 4, 5), _IDENTITY, 14)
    fresh_parent = measured.snapshot()
    # The unrelated wheel TF is still stale, but the visual's own sources are fresh.
    assert fresh_parent["status"] == "stale"
    assert fresh_parent["links"]["gear"]["position"] == pytest.approx([3.1, 4.2, 5.4])
    frozen = fresh_parent["links"]["gear"]
    wall[0] += 1.1
    measured.update_transform("cadiste", "crawler", (10, 20, 30), _IDENTITY, 15)
    measured.update_transform("crawler", "crawler/base", (0, 0, 0.1), _IDENTITY, 15)
    assert measured.snapshot()["links"]["gear"] == frozen


def test_invalid_visual_source_keeps_only_last_pose_and_out_of_order_cannot_revive_it(derived_state):
    measured, _ = derived_state
    _populate(measured)
    before = measured.snapshot()
    measured.update_transform("cadiste", "crawler", (math.nan, 0, 0), _IDENTITY, 14)
    measured.update_transform("cadiste", "crawler", (20, 30, 40), _IDENTITY, 13)
    measured.update_joints(["motor", "motor"], [3, 4], [5, 6], [], 14)
    measured.update_joints(["motor"], [100], [200], [300], 13)
    invalid = measured.snapshot()
    assert invalid["status"] == "stale"
    assert invalid["links"]["gear"] == before["links"]["gear"]
    assert "crawler" in invalid["missing_frames"]
    assert invalid["joints"]["drive_motor"] == dict.fromkeys(("position", "velocity", "effort"))
    measured.update_clock(0)
    reset = measured.snapshot()
    assert reset["status"] == "waiting"
    assert reset["links"] == {}
    assert reset["stamp"] is None
    assert reset["age"] is None
    assert reset["joints"]["drive_motor"] == dict.fromkeys(("position", "velocity", "effort"))
    _populate(measured, 0.1)
    assert measured.snapshot()["status"] == "live"
    assert "gear" in measured.snapshot()["links"]


@pytest.mark.parametrize("position", [math.nan, math.inf, -math.inf])
def test_nonfinite_joint_source_never_animates_a_visual(derived_state, position):
    measured, _ = derived_state
    _populate(measured)
    old = measured.snapshot()
    measured.update_joints(["motor"], [position], [2], [], 13)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "stale"
    assert snapshot["links"]["gear"] == old["links"]["gear"]
    assert snapshot["joints"]["drive_motor"] == {"position": None, "velocity": -5.0, "effort": None}
    json.dumps(snapshot, allow_nan=False)


@pytest.mark.parametrize("position,velocity,effort", [
    (math.nan, math.inf, math.nan), (1e308, -1e308, 1e308),
])
def test_nonfinite_or_overflowed_motor_values_never_escape_json(position, velocity, effort):
    config = _config()
    config["transmissions"] = [
        {"name": "fast_input", "joint": "motor", "ratio": -1e-308},
        {"name": "high_torque_input", "joint": "motor", "ratio": -1e308},
    ]
    measured = Measurements(config, monotonic=lambda: 100.0)
    _populate(measured)
    measured.update_joints(["motor"], [position], [velocity], [effort], 13)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "waiting"
    assert snapshot["joints"]["fast_input"]["position"] is None
    assert snapshot["joints"]["fast_input"]["velocity"] is None
    assert snapshot["joints"]["high_torque_input"]["effort"] is None
    json.dumps(snapshot, allow_nan=False)


@pytest.mark.parametrize("overflow", ["angle", "translation"])
def test_visual_overflow_is_not_a_pose_or_nonfinite_json(derived_state, overflow):
    measured, _ = derived_state
    _populate(measured)
    if overflow == "angle":
        measured.update_joints(["motor"], [1e308], [0], [], 13)
    else:
        measured.visuals["gear"]["pose"]["position"] = [1e308, 0, 0]
        measured.update_transform("cadiste", "crawler", (1e308, 0, 0), _IDENTITY, 13)
    snapshot = measured.snapshot()
    assert snapshot["status"] == "waiting"
    assert "gear" not in snapshot["links"]
    json.dumps(snapshot, allow_nan=False)


def test_bridge_initial_error_includes_null_virtual_motor_keys():
    config = _config()
    config["transmissions"] = [{"name": "drive_motor", "joint": "motor", "ratio": -0.4}]
    with Feedback(("/no/such/cadiste-ros-runtime",), config) as feedback:
        event = next(feedback.events())
    assert event["status"] == "error"
    assert event["joints"]["drive_motor"] == dict.fromkeys(("position", "velocity", "effort"))




def _child(code: str) -> tuple[str, ...]:
    # Real subprocesses test pipe ownership and error propagation, without ROS.
    return (sys.executable, "-u", "-c", code)


def test_bridge_missing_executable_is_explicit_error_and_close_is_idempotent():
    with Feedback(("/no/such/cadiste-ros-runtime",), _config()) as feedback:
        event = next(feedback.events())
        assert event["status"] == "error"
        assert "Could not start" in event["error"]
    feedback.close()


def test_bridge_reports_real_child_exit_and_stderr():
    code = "import sys; sys.stderr.write('package cadiste_ros not found\\n'); sys.exit(7)"
    with Feedback(_child(code), _config()) as feedback:
        events = list(feedback.events())
    assert events[-1]["status"] == "error"
    assert "package cadiste_ros not found" in events[-1]["error"]


def test_bridge_rejects_non_json_stdout_and_closes_child():
    with Feedback(_child("print('not json'); import sys; sys.stdin.read()"), _config()) as feedback:
        events = list(feedback.events())
    assert events[-1]["status"] == "error"
    assert "Invalid JSON" in events[-1]["error"]


def test_worker_immediate_stdin_eof_explains_required_forwarding():
    result = subprocess.run(
        [sys.executable, str(_WORKER), "--config", json.dumps(_config())],
        input="", capture_output=True, text=True, timeout=5, check=False,
    )
    assert result.returncode == 1
    snapshot = json.loads(result.stdout)
    assert snapshot["status"] == "error"
    assert "docker exec -i" in snapshot["error"]


def test_bridge_real_pipe_lifetime_uses_start_then_eof_not_terminate(tmp_path):
    marker = tmp_path / "graceful"
    # The child executes the production accumulator on actual numeric input;
    # this is a transport test, not a mock command/setpoint echo.
    code = f"""
import json, pathlib, runpy, sys
assert sys.stdin.readline() == 'start\\n'
config = json.loads(sys.argv[-1])
Measurements = runpy.run_path({str(_WORKER)!r})['Measurements']
state = Measurements(config)
state.update_transform('cadiste', 'crawler', (1, 2, 3), (0, 0, 0, 1), 2)
state.update_transform('crawler', 'crawler/base', (0, 0, 1), (0, 0, 0, 1), 2)
state.update_transform('crawler/base', 'crawler/wheel', (0.5, 0, 0), (0, 0, 0, 1), 2)
state.update_joints(['motor', 'steer'], [1, 2], [3, 4], [], 2)
print(json.dumps(state.snapshot()), flush=True)
sys.stdin.read()
pathlib.Path({str(marker)!r}).write_text('EOF')
"""
    with Feedback(_child(code), _config()) as feedback:
        stream = feedback.events()
        while True:
            snapshot = next(stream)
            if snapshot["status"] != "waiting":
                break
        assert snapshot["status"] == "live"
        assert snapshot["links"]["wheel"]["position"] == [1.5, 2.0, 4.0]
        assert snapshot["joints"]["motor"]["velocity"] == 3
    stream.close()
    assert marker.read_text() == "EOF"
