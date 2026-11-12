"""Captured subprocesses with cooperative cancellation for background CAD work."""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from collections.abc import Iterator


def run(
    command: list[str], *, env: dict[str, str], timeout: float, cancel: threading.Event | None = None
) -> subprocess.CompletedProcess[bytes]:
    if cancel is None:
        return subprocess.run(command, capture_output=True, timeout=timeout, check=False, env=env)
    if cancel.is_set():
        raise InterruptedError("background build stopped")
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) as process:
        try:
            while True:
                if cancel.is_set():
                    raise InterruptedError("background build stopped")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()


@contextlib.contextmanager
def locked(lock: threading.Lock, cancel: threading.Event | None = None) -> Iterator[None]:
    """Acquire shared catalog/cache locks without trapping a stopping background worker."""
    if cancel is None:
        lock.acquire()
    else:
        while not lock.acquire(timeout=0.2):
            if cancel.is_set():
                raise InterruptedError("background build stopped")
    try:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("background build stopped")
        yield
    finally:
        lock.release()
