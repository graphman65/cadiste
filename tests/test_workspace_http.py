from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from cadiste.cache import TraceCache
from cadiste.hub import Hub
from cadiste.profiles import Profile
from cadiste.server import Builds, Project, _Handler
from cadiste.workspace import Workspace


@pytest.fixture
def workspace_http(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    root = tmp_path / "projects"
    root.mkdir()
    workspace = Workspace(root)
    project = Project("", (), ())
    builds = Builds(project, TraceCache(tmp_path / "cache", [], ""), False)
    handler = type(
        "WorkspaceHandler",
        (_Handler,),
        {
            "workspace": workspace,
            "project": project,
            "builds": builds,
            "hub": Hub(),
            "workspace_only": True,
        },
    )
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", root
        finally:
            server.shutdown()
            thread.join()
            workspace.close()


def _request(url: str, path: str, body: Any = None, **headers: str) -> tuple[int, dict[str, Any]]:
    content = json.dumps(body).encode() if body is not None else None
    if content is not None:
        headers.setdefault("Content-Type", "application/json")
    request = Request(url + path, data=content, headers=headers)
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Origin": "http://evil.example"}, 403),
        ({"Origin": "null"}, 403),
        ({"Host": "evil.example", "Origin": "http://evil.example"}, 403),
        ({"Content-Type": "text/plain"}, 415),
    ],
)
def test_untrusted_browser_requests_cannot_create_project_files(workspace_http, headers, status):
    url, root = workspace_http
    result, _ = _request(url, "/api/workspace/create", {"path": "rejected", "name": "Rejected"}, **headers)
    assert result == status
    assert not (root / "rejected").exists()


def test_local_project_creation_never_overwrites_an_existing_project(workspace_http):
    url, root = workspace_http
    status, result = _request(url, "/api/workspace/create", {"path": "starter", "name": "Starter"})
    assert status == 200
    profile_path = Path(result["profile_path"])
    assert Profile.load(profile_path).root == root / "starter"
    model = root / "starter" / "model.py"
    original = model.read_bytes()
    status, _ = _request(url, "/api/workspace/create", {"path": "starter", "name": "Replacement"})
    assert status == 400
    assert model.read_bytes() == original


def test_workspace_http_rejects_escape_and_dns_rebinding_reads(workspace_http):
    url, _ = workspace_http
    assert _request(url, "/api/workspace/browse?path=..")[0] == 400
    assert _request(url, "/api/workspace", Host="evil.example")[0] == 403


def test_workspace_restart_rejects_unowned_process_identity(workspace_http):
    url, _ = workspace_http
    status, result = _request(url, "/api/workspace/restart", {"id": "external-process"})
    assert status == 400
    assert "external processes are never restarted" in result["error"]


def test_bridge_workspace_empty_error_contains_project_recovery(workspace_http):
    pytest.importorskip("mcp")
    from cadiste.bridge import Bridge

    url, _ = workspace_http
    bridge = Bridge(url)
    try:
        result = bridge.call("workspace-client", "list_parts", {})
        assert result.is_error
        assert result.structured_content["code"] == "workspace_empty"
        assert result.structured_content["details"]["workspace_url"] == url
        listed = bridge.call("workspace-client", "projects", {})
        assert listed.structured_content["workspace"]["children"] == []
        assert listed.structured_content["selected"]["endpoint"] == url
    finally:
        bridge.close()
