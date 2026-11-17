from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cadiste.cache import fingerprint
from cadiste.ros import Controls
from cadiste.runtime import RuntimeManager, inspect_simulation

TARGET = {"kind": "assembly", "name": "Robot"}


def metadata() -> dict[str, Any]:
    pose = {"position": [0, 0, 0], "quaternion": [0, 0, 0, 1]}
    return {
        "model": "robot",
        "commands": [],
        "joint_states": "/robot/joint_states",
        "tf": "/tf",
        "clock": "/clock",
        "launch": "ros2 launch ./sim.launch.py",
        "runtime": "ROS 2 Jazzy / Gazebo Harmonic",
        "transmissions": [],
        "feedback": {
            "world_frame": "cadiste",
            "model_frame": "robot",
            "links": {"body": {"frame": "robot/body", "rest": copy.deepcopy(pose)}},
            "joints": [],
            "visuals": {},
        },
        "topology": {
            "links": [{"name": "body", "owner": "robot", "pose": pose}],
            "joints": [],
            "environment": [{"name": "floor", "owner": "environment", "static": True, "pose": copy.deepcopy(pose)}],
        },
    }


class Builds:
    def __init__(self, path: Path):
        source = path / "source"
        source.mkdir()
        self.source = source / "model.py"
        self.source.write_text("size = 1\n")
        self.cache = SimpleNamespace(directory=path / "cache", roots=[source], salt="test")
        self.cache.fingerprint = lambda: fingerprint(self.cache.roots, self.cache.salt)
        self.config = metadata()
        self.export_failure: dict[str, str] = {}

    def gazebo(self, target: dict[str, str], output: Path) -> dict[str, str]:
        assert target == TARGET
        if not self.export_failure:
            (output / "control.json").write_text(json.dumps(self.config))
        return self.export_failure


class Process:
    pid = 12345

    def __init__(self):
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        assert self.returncode is not None
        return self.returncode


def test_read_only_inspection_needs_no_ros_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    builds = Builds(tmp_path)
    controls = Controls(lambda target: builds.config)

    def forbidden(*args: Any, **kwargs: Any):
        pytest.fail("read-only inspection attempted runtime execution")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("cadiste.runtime.shutil.which", lambda executable: None)
    result = inspect_simulation(builds, controls, TARGET)
    assert result["config"]["topology"]["environment"][0]["owner"] == "environment"
    assert result["measurement"]["status"] == "not_observed"
    assert result["prerequisites"]["status"] == "unavailable"
    assert result["source_changed_during_inspection"] is False


def test_owned_lifecycle_and_stale_source_are_separate_from_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    builds = Builds(tmp_path)
    controls = Controls(lambda target: builds.config)
    manager = RuntimeManager(builds, controls)
    children: list[Process] = []
    signals: list[tuple[int, int]] = []

    def launch(output: Path, *, gui: bool):
        child = Process()
        children.append(child)
        (output / "gazebo.log").write_text("Gazebo starting\n")
        return child

    def killpg(pid: int, sig: int):
        assert pid == children[-1].pid
        signals.append((pid, sig))
        children[-1].returncode = -sig

    monkeypatch.setattr("cadiste.simulation.launch_bundle", launch)
    monkeypatch.setattr("cadiste.runtime.os.killpg", killpg)
    assert manager.stop(TARGET)["status"] == "not_started"
    assert not signals  # An external runtime is never treated as owned.
    started = manager.start(TARGET, gui=False)
    assert started["owned"] is True
    assert started["status"] == "launched"
    assert started["measurement"]["status"] == "not_observed"
    assert started["mismatch"] == {"source": False, "config": False}
    with pytest.raises(ValueError, match="explicitly relaunch"):
        manager.start(TARGET)
    builds.source.write_text("size = 1000\n")
    changed = manager.status(TARGET)
    assert changed["mismatch"] == {"source": True, "config": False}
    assert changed["active"]["source_fingerprint"] == started["active"]["source_fingerprint"]
    relaunched = manager.relaunch(TARGET, gui=False)
    assert len(children) == 2
    assert len(signals) == 1
    assert relaunched["active"]["bundle_id"] != started["active"]["bundle_id"]
    assert relaunched["mismatch"] == {"source": False, "config": False}
    manager.close()
    assert len(signals) == 2
    assert manager.status(TARGET)["status"] == "stopped"
    assert Path(started["directory"]).is_dir()


def test_export_and_launch_failures_retain_actionable_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    builds = Builds(tmp_path)
    manager = RuntimeManager(builds, Controls(lambda target: builds.config))
    builds.export_failure = {"error": "adapter failed", "log": "Traceback: missing wheel"}
    failed = manager.start(TARGET)
    assert failed["status"] == "failed"
    assert failed["owned"] is False
    assert "missing wheel" in failed["logs"]["text"]
    first_log = Path(failed["log"])
    builds.export_failure = {}

    def unavailable(*args: Any, **kwargs: Any):
        raise RuntimeError("ROS 2 package ros_gz_sim is unavailable")

    monkeypatch.setattr("cadiste.simulation.launch_bundle", unavailable)
    failed = manager.relaunch(TARGET)
    assert failed["status"] == "failed"
    assert "ros_gz_sim" in failed["logs"]["text"]
    assert first_log.is_file()
    assert Path(failed["log"]).is_file()
    manager.close()


def test_log_tail_is_bounded_and_exit_is_not_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    builds = Builds(tmp_path)
    manager = RuntimeManager(builds, Controls(lambda target: builds.config))
    process = Process()
    monkeypatch.setattr("cadiste.simulation.launch_bundle", lambda output, *, gui: process)
    launched = manager.start(TARGET)
    Path(launched["log"]).write_text("".join(f"row {index}\n" for index in range(12000)))
    tail = manager.logs(TARGET, lines=2)
    assert tail["text"] == "row 11998\nrow 11999"
    assert tail["truncated"] is True
    process.returncode = 2
    exited = manager.status(TARGET)
    assert exited["status"] == "failed"
    assert exited["exit_code"] == 2
    assert exited["measurement"]["status"] == "not_observed"
    with pytest.raises(ValueError, match="log lines"):
        manager.logs(TARGET, lines=True)


def test_source_mismatch_survives_broken_current_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    builds = Builds(tmp_path)
    manager = RuntimeManager(builds, Controls(lambda target: builds.config))
    process = Process()
    monkeypatch.setattr("cadiste.simulation.launch_bundle", lambda output, *, gui: process)
    original = manager.start(TARGET)
    builds.source.write_text("broken new source\\n")
    builds.config.pop("model")
    status = manager.status(TARGET)
    assert status["inspection_error"]
    assert status["mismatch"] == {"source": True, "config": None}
    assert status["active"]["bundle_id"] == original["active"]["bundle_id"]
    assert status["status"] == "launched"
    process.returncode = 0
    manager.close()


def test_unreaped_launcher_is_not_reported_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import subprocess

    builds = Builds(tmp_path)
    manager = RuntimeManager(builds, Controls(lambda target: builds.config))
    process = Process()
    monkeypatch.setattr("cadiste.simulation.launch_bundle", lambda output, *, gui: process)
    monkeypatch.setattr("cadiste.runtime.os.killpg", lambda pid, sig: None)

    def blocked_wait(timeout: float | None = None):
        raise subprocess.TimeoutExpired("ros2 launch", timeout or 0)

    monkeypatch.setattr(process, "wait", blocked_wait)
    manager.start(TARGET)
    result = manager.stop(TARGET)
    assert result["status"] == "failed"
    assert result["exit_code"] is None
    assert "may still be running" in result["error"]
    process.returncode = 0
    manager.close()


def test_lifecycle_stops_real_owned_child_but_not_external_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import subprocess
    import sys

    builds = Builds(tmp_path)
    manager = RuntimeManager(builds, Controls(lambda target: builds.config))
    children: list[subprocess.Popen[bytes]] = []
    external = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def harmless_launch(output: Path, *, gui: bool):
        with (output / "gazebo.log").open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        children.append(child)
        return child

    monkeypatch.setattr("cadiste.simulation.launch_bundle", harmless_launch)
    try:
        launched = manager.start(TARGET, gui=False)
        assert launched["status"] == "launched"
        assert launched["pid"] == children[0].pid
        assert launched["measurement"]["status"] == "not_observed"
        stopped = manager.stop(TARGET)
        assert stopped["status"] == "stopped"
        assert children[0].poll() is not None
        assert external.poll() is None
        relaunched = manager.relaunch(TARGET, gui=False)
        assert relaunched["status"] == "launched"
        assert relaunched["pid"] == children[1].pid
        assert relaunched["active"]["bundle_id"] != launched["active"]["bundle_id"]
        manager.close()
        assert children[1].poll() is not None
        assert external.poll() is None
    finally:
        manager.close()
        external.terminate()
        external.wait(timeout=3)


def test_observed_feedback_ages_to_stale_without_fabricating_measurements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    builds = Builds(tmp_path)
    manager = RuntimeManager(builds, Controls(lambda target: builds.config))
    now = [100.0]
    monkeypatch.setattr("cadiste.runtime.time.monotonic", lambda: now[0])
    missing = manager.measurement(TARGET)
    assert missing["status"] == "not_observed"
    assert missing["observed"] is False
    assert missing["age"] is None and missing["received_age"] is None
    payload = {
        "status": "live",
        "model": "robot",
        "stamp": 5.0,
        "age": 0.1,
        "joints": {"drive": {"position": 0.4, "velocity": 0.2, "effort": None}},
        "links": {},
        "model_pose": None,
        "missing_frames": [],
    }
    manager.observe_feedback(TARGET, payload)
    payload["joints"]["drive"]["position"] = 999
    now[0] = 100.5
    live = manager.status(TARGET)["measurement"]
    assert live["status"] == "live" and live["reported_status"] == "live"
    assert live["observed"] is True
    assert live["received_age"] == pytest.approx(0.5)
    assert live["age"] == pytest.approx(0.6)
    assert live["joints"]["drive"]["position"] == 0.4
    live["joints"]["drive"]["position"] = 999
    now[0] = 101.1
    stale = manager.measurement(TARGET)
    assert stale["status"] == "stale" and stale["reported_status"] == "live"
    assert stale["age"] == pytest.approx(1.2)
    assert stale["stamp"] == 5.0
    assert stale["joints"]["drive"] == {"position": 0.4, "velocity": 0.2, "effort": None}
    assert manager.measurement({"kind": "assembly", "name": "Other"})["status"] == "not_observed"
    manager.observe_feedback(TARGET, {**payload, "status": "waiting", "age": None, "stamp": None})
    now[0] = 103.0
    waiting = manager.measurement(TARGET)
    assert waiting["status"] == "waiting"
    assert waiting["age"] is None and waiting["stamp"] is None
    manager.observe_feedback(TARGET, {**payload, "age": 0.9})
    now[0] = 103.2
    assert manager.measurement(TARGET)["status"] == "stale"  # Old samples, even on a recent receipt.
    manager.observe_feedback(TARGET, {**payload, "status": "error", "error": "ROS worker disconnected"})
    now[0] = 105.0
    errored = manager.measurement(TARGET)
    assert errored["status"] == "error"
    assert errored["error"] == "ROS worker disconnected"


def test_fresh_old_config_subscriber_is_not_labelled_current_after_cad_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    builds = Builds(tmp_path)
    manager = RuntimeManager(builds, Controls(lambda target: builds.config))
    monkeypatch.setattr("cadiste.runtime.time.monotonic", lambda: 100.0)
    original_id = manager.status(TARGET)["current"]["config_id"]
    payload = {
        "status": "live",
        "model": "robot",
        "stamp": 5.0,
        "age": 0.1,
        "joints": {"drive": {"position": 0.4, "velocity": 0.2, "effort": None}},
        "links": {},
        "model_pose": None,
        "missing_frames": [],
    }
    manager.observe_feedback(TARGET, payload, config_id=original_id)
    matching = manager.status(TARGET)["measurement"]
    assert matching["status"] == "live" and matching["config_mismatch"] is False
    assert matching["config_id"] == matching["current_config_id"] == original_id
    builds.source.write_text("size = 10\\n")
    builds.config["topology"]["links"][0]["pose"]["position"][0] = 0.1
    changed = manager.status(TARGET)
    new_id = changed["current"]["config_id"]
    assert new_id != original_id
    manager.observe_feedback(TARGET, payload, config_id=original_id)  # The old SSE stream still emits fresh samples.
    old_stream = manager.status(TARGET)["measurement"]
    assert old_stream["config_id"] == original_id
    assert old_stream["current_config_id"] == new_id
    assert old_stream["config_mismatch"] is True
    assert old_stream["status"] == "stale" and old_stream["reported_status"] == "live"
    assert old_stream["age"] == pytest.approx(0.1)
    assert old_stream["joints"]["drive"]["position"] == 0.4
    assert "older/different controls configuration" in old_stream["detail"]
    manager.observe_feedback(TARGET, payload)
    assert manager.status(TARGET)["measurement"]["config_mismatch"] is None
    manager.observe_feedback(TARGET, payload, config_id=new_id)
    refreshed = manager.status(TARGET)["measurement"]
    assert refreshed["status"] == "live" and refreshed["config_mismatch"] is False
