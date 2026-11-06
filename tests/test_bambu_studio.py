from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from build123d import import_step

from cadiste.cache import TraceCache
from cadiste.server import Builds, Project, _Handler


@pytest.fixture
def slicer_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path]]:
    model = tmp_path / "bambu_model.py"
    model.write_text(
        "from build123d import Box\n"
        "def build(name):\n"
        "    if name == 'broken':\n"
        "        raise ValueError('unbuildable part')\n"
        "    return Box(10, 10, 10) if name == 'cube' else Box(20, 4, 6)\n"
    )
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(tmp_path), os.environ.get("PYTHONPATH", "")])))
    project = Project(
        "bambu_model:build",
        (),
        ("cube", "bar", "broken"),
        folders={name: [{"name": "parts", "file": True}] for name in ("cube", "bar", "broken")},
    )
    cache = TraceCache(tmp_path / "cache", [tmp_path], "")
    handler = type("SlicerHandler", (_Handler,), {"builds": Builds(project, cache, always_fresh=False)})
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", cache.directory
        finally:
            server.shutdown()
            thread.join()


def _post(url: str, body: object, content_type: str = "application/json") -> tuple[int, Any]:
    request = Request(url + "/api/bambu-studio", data=json.dumps(body).encode(), headers={"Content-Type": content_type})
    try:
        with urlopen(request, timeout=120) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


@pytest.fixture
def launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen[Any]]:
    """A real executable consumes the STEP file after handoff, without needing a GUI in the test suite."""
    executable = tmp_path / "bambustudio"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "from build123d import import_step\n"
        "path = Path(sys.argv[1])\n"
        "size = import_step(path).bounding_box().size\n"
        "path.with_suffix('.json').write_text(json.dumps([size.X, size.Y, size.Z]))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    processes: list[subprocess.Popen[Any]] = []
    popen = subprocess.Popen

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        process = popen(command, **kwargs)
        if command[0] == str(executable):
            processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    return processes


def test_slicer_receives_only_shown_geometry_and_each_export_remains_reloadable(
    slicer_server: tuple[str, Path], launcher: list[subprocess.Popen[Any]]
):
    url, cache = slicer_server
    for part in ("cube", "bar"):
        status, body = _post(url, {"target": {"kind": "file", "path": "parts"}, "parts": [part]})
        assert (status, body) == (200, {})
        assert launcher[-1].wait(timeout=10) == 0
    exports = sorted((cache / "bambu-studio").glob("*.step"))
    dimensions = sorted(json.loads(path.with_suffix(".json").read_text()) for path in exports)
    assert dimensions == [[10, 10, 10], [20, 4, 6]]
    # Both source files survive the HTTP requests and the consuming processes.
    volumes = sorted(sum(solid.volume for solid in import_step(path).solids()) for path in exports)
    assert volumes == pytest.approx([480, 1000])


def test_build_failure_reports_traceback_without_starting_slicer(
    slicer_server: tuple[str, Path], launcher: list[subprocess.Popen[Any]]
):
    url, cache = slicer_server
    status, body = _post(url, {"target": {"kind": "part", "name": "broken"}})
    assert status == 500
    assert "ValueError: unbuildable part" in body["log"]
    assert launcher == []
    assert not (cache / "bambu-studio").exists()


def test_missing_local_application_has_actionable_error(
    slicer_server: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PATH", "")
    status, body = _post(slicer_server[0], {"target": {"kind": "part", "name": "cube"}})
    assert status == 503
    assert "Bambu Studio was not found on the server's PATH" in body["error"]


def test_unstartable_application_reports_error_and_discards_failed_handoff(
    slicer_server: tuple[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = tmp_path / "bambustudio"
    executable.write_text("#!/nonexistent/bambu-interpreter\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    url, cache = slicer_server
    status, body = _post(url, {"target": {"kind": "part", "name": "cube"}})
    assert status == 500
    assert "could not open Bambu Studio:" in body["error"]
    assert list((cache / "bambu-studio").iterdir()) == []


@pytest.mark.parametrize(
    ("body", "content_type", "status"),
    [
        ({"target": {"kind": "part", "name": "cube"}}, "text/plain", 415),
        ({"target": {"kind": "part", "name": "cube"}}, "application/x-www-form-urlencoded", 415),
        ({"target": {"kind": "file", "path": "parts"}, "parts": []}, "application/json", 400),
        ({"target": {"kind": "assembly", "name": []}}, "application/json", 400),
        ([], "application/json", 400),
    ],
)
def test_unsafe_or_invalid_requests_cannot_launch_app(
    slicer_server: tuple[str, Path],
    launcher: list[subprocess.Popen[Any]],
    body: object,
    content_type: str,
    status: int,
):
    assert _post(slicer_server[0], body, content_type)[0] == status
    assert launcher == []
