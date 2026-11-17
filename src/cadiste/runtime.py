"""Read-only simulation inspection and explicitly owned native Gazebo launch lifetimes.

A launcher being alive is not ROS feedback or proof of simulator health. External ROS
processes and containers are never discovered as owned, stopped, or restarted here.
"""

from __future__ import annotations

import contextlib
import copy
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .feedback import STALE_AFTER
from .ros import Controls


def _target(target: dict[str, str]) -> dict[str, str]:
    if (
        not isinstance(target, dict)
        or set(target) != {"kind", "name"}
        or target["kind"] != "assembly"
        or not isinstance(target["name"], str)
        or not target["name"].strip()
    ):
        raise ValueError("expected an assembly target with kind and nonempty name")
    return {"kind": "assembly", "name": target["name"]}


def _source_identity(builds: Any) -> str:
    # This is the existing conservative cache fingerprint, not the captured Python
    # source_revision and not a geometry/build identity.
    return builds.cache.fingerprint()


def _prerequisites() -> dict[str, Any]:
    executable = shutil.which("ros2")
    return {
        "ros2": executable,
        "status": "unverified" if executable else "unavailable",
        "detail": (
            "ros2 found on the server PATH; packages/plugins are checked only when explicitly launching. "
            "This is not a runtime health check."
            if executable
            else "Native launch needs ROS 2 Jazzy and ros-jazzy-ros-gz; "
            "source /opt/ros/jazzy/setup.bash before starting Cadiste. "
            "An externally managed runtime can still supply measured feedback through the configured ROS command."
        ),
    }


def inspect_simulation(builds: Any, controls: Controls, target: dict[str, str]) -> dict[str, Any]:
    """Inspect declared topology/commands without launching, importing ROS, or publishing."""
    canonical = _target(target)
    before = _source_identity(builds)
    config = controls.describe(canonical)
    after = _source_identity(builds)
    return {
        "target": canonical,
        "config": config,
        "current": {"config_id": config["config_id"], "source_fingerprint": after},
        "source_changed_during_inspection": before != after,
        "prerequisites": _prerequisites(),
        "measurement": {
            "status": "not_observed",
            "detail": "Declared CAD metadata only. Measured live state requires the separate ROS feedback stream.",
        },
    }


@dataclass
class _Launch:
    directory: Path
    identity: dict[str, Any]
    gui: bool
    process: subprocess.Popen[bytes] | None = None
    state: str = "preparing"
    error: str | None = None


class RuntimeManager:
    """One explicit launcher per assembly; only children launched here can be stopped.

    Bundle directories and logs are retained after failures and stops. The server
    owns this manager and calls close() at shutdown, never on panel/view changes.
    """

    def __init__(self, builds: Any, controls: Controls):
        self.builds = builds
        self.controls = controls
        self._launches: dict[str, _Launch] = {}
        self._inspections: dict[str, tuple[str, dict[str, Any]]] = {}
        self._observations: dict[str, tuple[float, dict[str, Any], str | None]] = {}
        self._lock = threading.RLock()
        self._closed = False

    def _inspect(self, target: dict[str, str]) -> dict[str, Any]:
        stamp = _source_identity(self.builds)
        cached = self._inspections.get(target["name"])
        if cached is not None and cached[0] == stamp:
            return cached[1]
        inspected = inspect_simulation(self.builds, self.controls, target)
        if inspected["source_changed_during_inspection"]:
            raise RuntimeError("project source changed while inspecting simulation; reload controls before launching")
        self._inspections[target["name"]] = (inspected["current"]["source_fingerprint"], inspected)
        return inspected

    def observe_feedback(
        self, target: dict[str, str], payload: dict[str, Any], *, config_id: str | None = None
    ) -> None:
        """Retain an actual existing SSE observation; never subscribe or command."""
        canonical = _target(target)
        received = time.monotonic()
        if not isinstance(payload, dict) or payload.get("status") not in {"waiting", "live", "stale", "error"}:
            raise ValueError("expected an actual ROS feedback snapshot")
        if config_id is not None and (not isinstance(config_id, str) or not config_id):
            raise ValueError("observed config_id must be a nonempty string or None")
        snapshot = copy.deepcopy(payload)
        with self._lock:
            previous = self._observations.get(canonical["name"])
            if previous is None or received >= previous[0]:
                self._observations[canonical["name"]] = (received, snapshot, config_id)

    def measurement(self, target: dict[str, str], *, current_config_id: str | None = None) -> dict[str, Any]:
        """Age only real observations using monotonic time, without runtime I/O."""
        canonical = _target(target)
        with self._lock:
            observation = self._observations.get(canonical["name"])
            if observation is None:
                return {
                    "status": "not_observed",
                    "observed": False,
                    "age": None,
                    "received_age": None,
                    "config_id": None,
                    "current_config_id": current_config_id,
                    "config_mismatch": None,
                    "detail": (
                        "No ROS feedback has been observed by this server. Launcher state is not measured feedback."
                    ),
                }
            received, snapshot, config_id = observation
            payload = copy.deepcopy(snapshot)
        elapsed = max(0.0, time.monotonic() - received)
        original_age = payload.get("age")
        age = (
            float(original_age) + elapsed
            if isinstance(original_age, (int, float))
            and not isinstance(original_age, bool)
            and math.isfinite(original_age)
            and original_age >= 0
            else None
        )
        reported = payload["status"]
        status = reported
        config_mismatch = None if config_id is None or current_config_id is None else config_id != current_config_id
        if reported == "live" and (
            elapsed > STALE_AFTER or (age is not None and age > STALE_AFTER) or config_mismatch is True
        ):
            status = "stale"
        return {
            **payload,
            "status": status,
            "reported_status": reported,
            "observed": True,
            "age": age,
            "received_age": elapsed,
            "config_id": config_id,
            "current_config_id": current_config_id,
            "config_mismatch": config_mismatch,
            "detail": (
                "Observed feedback belongs to an older/different controls configuration; "
                "it is not current for this CAD config."
                if config_mismatch
                else "Last observed ROS feedback, aged since receipt; "
                "not proof that it matches the active bundle or current CAD."
            ),
        }

    def logs(self, target: dict[str, str], *, lines: int = 100) -> dict[str, Any]:
        canonical = _target(target)
        if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= 1000:
            raise ValueError("log lines must be an integer between 1 and 1000")
        with self._lock:
            launch = self._launches.get(canonical["name"])
            if launch is None:
                return {"path": None, "text": "", "truncated": False}
            path = launch.directory / "gazebo.log"
            try:
                with path.open("rb") as stream:
                    size = stream.seek(0, os.SEEK_END)
                    offset = max(0, size - 65536)
                    stream.seek(offset)
                    data = stream.read(65536)
            except FileNotFoundError:
                return {"path": str(path), "text": "", "truncated": False}
            if offset:
                # Discard a partial first line when starting inside a long file.
                _, separator, rest = data.partition(b"\n")
                data = rest if separator else data
            rows = data.decode("utf-8", errors="replace").splitlines()
            return {"path": str(path), "text": "\n".join(rows[-lines:]), "truncated": bool(offset or len(rows) > lines)}

    def status(self, target: dict[str, str]) -> dict[str, Any]:
        canonical = _target(target)
        with self._lock:
            current = None
            inspection_error = None
            try:
                current = dict(self._inspect(canonical)["current"])
            except (ValueError, LookupError, OSError, RuntimeError) as error:
                inspection_error = str(error)
                with contextlib.suppress(OSError):
                    current = {"config_id": None, "source_fingerprint": _source_identity(self.builds)}
            launch = self._launches.get(canonical["name"])
            active = None
            exit_code = None
            state = "not_started"
            if launch is not None:
                if launch.process is not None:
                    exit_code = launch.process.poll()
                    if exit_code is not None and launch.state == "launched":
                        launch.state = "exited" if exit_code == 0 else "failed"
                        if exit_code:
                            launch.error = f"ROS 2 launcher exited with status {exit_code}; inspect the retained log."
                state = launch.state
                active = {**launch.identity, "directory": str(launch.directory), "gui": launch.gui}
            mismatch = (
                None
                if active is None or current is None
                else {
                    "source": active["source_fingerprint"] != current["source_fingerprint"],
                    "config": None if current["config_id"] is None else active["config_id"] != current["config_id"],
                }
            )
            return {
                "target": canonical,
                "status": state,
                "owned": launch is not None and launch.process is not None,
                "pid": launch.process.pid if launch is not None and launch.process is not None else None,
                "exit_code": exit_code,
                "active": active,
                "current": current,
                "mismatch": mismatch,
                "inspection_error": inspection_error,
                "error": launch.error if launch is not None else None,
                "prerequisites": _prerequisites(),
                "logs": self.logs(canonical),
                "measurement": self.measurement(
                    canonical, current_config_id=None if current is None else current["config_id"]
                ),
                # Preserve the existing explicit Gazebo handoff response fields.
                "directory": str(launch.directory) if launch is not None else None,
                "log": str(launch.directory / "gazebo.log") if launch is not None else None,
            }

    @staticmethod
    def _failure(launch: _Launch, error: str, detail: str = "") -> None:
        launch.state, launch.error = "failed", error
        with (launch.directory / "gazebo.log").open("a") as log:
            log.write(f"\nCadiste: {error}\n")
            if detail:
                log.write(detail + "\n")

    def start(self, target: dict[str, str], *, gui: bool = True) -> dict[str, Any]:
        canonical = _target(target)
        if not isinstance(gui, bool):
            raise ValueError("gui must be a boolean")
        with self._lock:
            if self._closed:
                raise RuntimeError("simulation runtime manager is closed")
            previous = self._launches.get(canonical["name"])
            if previous is not None and previous.process is not None and previous.process.poll() is None:
                raise ValueError("this assembly already has an owned launcher; explicitly relaunch or stop it")
            inspected = self._inspect(canonical)
            root = self.builds.cache.directory / "gazebo"
            root.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="model-", dir=root)).resolve()
            launch = _Launch(
                directory, {**inspected["current"], "bundle_id": directory.name, "started_at": time.time()}, gui
            )
            self._launches[canonical["name"]] = launch
            try:
                failure = self.builds.gazebo(canonical, directory)
                if failure:
                    self._failure(
                        launch, failure.get("error", "simulation bundle export failed"), failure.get("log", "")
                    )
                    return self.status(canonical)
                bundle = json.loads((directory / "control.json").read_text())
                expected = {
                    key: value for key, value in inspected["config"].items() if key not in {"config_id", "target"}
                }
                if bundle != expected or _source_identity(self.builds) != launch.identity["source_fingerprint"]:
                    raise RuntimeError(
                        "simulation source/config changed during bundle export; reload and explicitly relaunch"
                    )
                (directory / "runtime.json").write_text(json.dumps(launch.identity, indent=2) + "\n")
                from .simulation import launch_bundle

                launch.process = launch_bundle(directory, gui=gui)
                launch.state = "launched"
            except (ValueError, TypeError, LookupError, OSError, RuntimeError) as error:
                self._failure(launch, str(error))
            return self.status(canonical)

    def stop(self, target: dict[str, str]) -> dict[str, Any]:
        canonical = _target(target)
        with self._lock:
            launch = self._launches.get(canonical["name"])
            if launch is not None and launch.process is not None and launch.process.poll() is None:
                process = launch.process
                # launch_bundle creates a new session. Its process group contains
                # only this owned launch and its children, never an external ROS runtime.
                for sig, timeout in ((signal.SIGINT, 5), (signal.SIGTERM, 3), (signal.SIGKILL, 3)):
                    if process.poll() is not None:
                        break
                    try:
                        os.killpg(process.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        process.wait(timeout=timeout)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if process.poll() is None:
                    self._failure(
                        launch, "Owned ROS 2 launcher did not exit after stop signals; its PID may still be running."
                    )
                else:
                    launch.state = "stopped"
            return self.status(canonical)

    def relaunch(self, target: dict[str, str], *, gui: bool = True) -> dict[str, Any]:
        if not isinstance(gui, bool):
            raise ValueError("gui must be a boolean")
        with self._lock:
            self.stop(target)
            self._inspections.pop(_target(target)["name"], None)
            return self.start(target, gui=gui)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for name in self._launches:
                self.stop({"kind": "assembly", "name": name})
