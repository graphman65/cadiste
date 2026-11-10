"""Bounded JSON-lines transport to ROS's own Python, with stdin-owned lifetime."""
from __future__ import annotations

import json
import math
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Generator
from contextlib import suppress
from typing import Any

_START_TIMEOUT = 15.0
_MAX_LINE = 1024 * 1024


def _empty(config: dict[str, Any]) -> dict[str, Any]:
    feedback = config["feedback"]
    return {
        "status": "waiting", "model": config["model"], "stamp": None, "age": None,
        "joints": {
            name: dict.fromkeys(("position", "velocity", "effort"))
            for name in [*feedback["joints"], *(item["name"] for item in config["transmissions"])]
        },
        "model_pose": None, "links": {},
        "missing_frames": sorted({feedback["model_frame"], *(item["frame"] for item in feedback["links"].values())}),
    }


def _valid_numbers(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_valid_numbers(item) for item in value.values())
    if isinstance(value, list):
        return all(_valid_numbers(item) for item in value)
    return True


class Feedback:
    """One ROS subscriber worker per live stream; never imports rclpy in Cadiste.

    Closing stdin reaches the remote worker even through ``docker exec -i``.
    Terminating the local CLI is only a last resort, not the remote cleanup path.
    """

    def __init__(self, ros_command: tuple[str, ...], config: dict[str, Any]):
        if not ros_command or any(not isinstance(arg, str) or not arg for arg in ros_command):
            raise ValueError("ros_command must contain an executable and nonempty arguments")
        self._command = ros_command
        self._config = json.loads(json.dumps(config, allow_nan=False))
        self._process: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2)
        self._stderr: deque[str] = deque(maxlen=16)
        self._stderr_lock = threading.Lock()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._stdout_done = threading.Event()
        self._threads: list[threading.Thread] = []
        self._failure: str | None = None
        self._started = False

    def __enter__(self) -> Feedback:
        self._start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _start(self) -> None:
        with self._lock:
            if self._started or self._closed.is_set():
                return
            self._started = True
            try:
                self._process = subprocess.Popen(
                    [*self._command, "run", "cadiste_ros", "feedback", "--config",
                     json.dumps(self._config, allow_nan=False, separators=(",", ":"))],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
            except OSError as error:
                self._failure = f"Could not start ROS feedback worker: {error}"
                return
            for name, target in (("stdout", self._read_stdout), ("stderr", self._read_stderr)):
                thread = threading.Thread(target=target, name=f"cadiste-feedback-{name}", daemon=True)
                self._threads.append(thread)
                thread.start()
            try:
                assert self._process.stdin is not None
                self._process.stdin.write("start\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                self._failure = (
                    f"ROS feedback worker closed stdin: {error}; use a stdin-forwarding prefix (docker exec -i)."
                )

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while not self._closed.is_set():
                line = self._process.stdout.readline(_MAX_LINE + 1)
                if not line:
                    break
                if len(line) > _MAX_LINE:
                    self._failure = "ROS feedback worker exceeded the JSON-line size limit"
                    break
                try:
                    value = json.loads(line)
                    if (not isinstance(value, dict) or value.get("model") != self._config["model"]
                            or value.get("status") not in {"waiting", "live", "stale", "error"}
                            or not set(_empty(self._config)).issubset(value) or not _valid_numbers(value)):
                        raise ValueError("unexpected feedback snapshot")
                except (ValueError, TypeError) as error:
                    self._failure = f"Invalid JSON from ROS feedback worker: {error}"
                    break
                try:
                    self._queue.put_nowait(value)
                except queue.Full:
                    with suppress(queue.Empty):
                        self._queue.get_nowait()
                    self._queue.put_nowait(value)
        except (OSError, ValueError) as error:
            if not self._closed.is_set():
                self._failure = f"Could not read ROS feedback: {error}"
        finally:
            self._stdout_done.set()

    def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            while True:
                chunk = self._process.stderr.read(1024)
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr.append(chunk)
        except (OSError, ValueError):
            return

    def _error(self, message: str, last: dict[str, Any]) -> dict[str, Any]:
        with self._stderr_lock:
            detail = "".join(self._stderr).strip()
        if detail:
            message += f": {detail}"
        return {**last, "status": "error", "error": message}

    def events(self) -> Generator[dict[str, Any]]:
        self._start()
        began = time.monotonic()
        last = _empty(self._config)
        arrival: float | None = None
        try:
            while not self._closed.is_set():
                if self._failure:
                    yield self._error(self._failure, last)
                    return
                try:
                    value = self._queue.get(timeout=0.1)
                except queue.Empty:
                    value = None
                if self._failure:
                    yield self._error(self._failure, last)
                    return
                now = time.monotonic()
                if value is not None:
                    last, arrival = value, now
                    yield value
                    if value["status"] == "error":
                        return
                    continue
                if self._stdout_done.is_set():
                    # Briefly allow process status and bounded diagnostic draining to settle.
                    process = self._process
                    if process is not None:
                        try:
                            code = process.wait(timeout=0.2)
                        except subprocess.TimeoutExpired:
                            code = process.poll()
                    else:
                        code = None
                    for thread in self._threads:
                        thread.join(timeout=0.1)
                    yield self._error(
                        f"ROS feedback worker exited (status {code}); ensure cadiste_ros is on AMENT_PREFIX_PATH "
                        "and the command forwards stdin (docker exec -i)", last,
                    )
                    return
                if arrival is None and now - began > _START_TIMEOUT:
                    yield self._error(
                        "ROS feedback worker did not produce a snapshot within 15 s; check its runtime prefix", last,
                    )
                    return
                heartbeat = dict(last)
                if arrival is not None:
                    elapsed = now - arrival
                    if last["age"] is not None:
                        heartbeat["age"] = last["age"] + elapsed
                    if elapsed > 1.0 or (heartbeat["age"] is not None and heartbeat["age"] > 1.0):
                        heartbeat["status"] = "stale" if last["status"] in {"live", "stale"} else "waiting"
                yield heartbeat
        finally:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            process = self._process
            if process is None:
                return
            if process.stdin is not None:
                with suppress(OSError):
                    process.stdin.close()
        # EOF is deliberately given time to stop the remote rclpy process first.
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        for thread in self._threads:
            thread.join(timeout=0.3)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
