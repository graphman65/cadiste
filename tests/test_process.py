from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from cadiste.process import locked, run


def test_cancelling_a_running_build_reaps_its_subprocess(tmp_path: Path):
    started = tmp_path / "started"
    cancel = threading.Event()
    failures: list[BaseException] = []

    def build():
        try:
            run(
                [
                    sys.executable,
                    "-c",
                    "import os, pathlib, sys, time; "
                    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)",
                    str(started),
                ],
                env=dict(os.environ),
                timeout=60,
                cancel=cancel,
            )
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=build)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists(), "child did not start"
        pid = int(started.read_text())
    finally:
        cancel.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], InterruptedError)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cancelled_cache_wait_does_not_release_another_builds_lock():
    lock = threading.Lock()
    cancel = threading.Event()
    cancel.set()
    with lock:
        with pytest.raises(InterruptedError), locked(lock, cancel):
            pytest.fail("cancelled build acquired the foreground build's lock")
        assert lock.locked()
    with locked(lock):
        assert lock.locked()
    assert not lock.locked()
