from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import URLError

import pytest

from cadiste.hub import Hub

pytest.importorskip("mcp")
ToolError = importlib.import_module("mcp.server.mcpserver.exceptions").ToolError
bridge_module = importlib.import_module("cadiste.bridge")
Bridge, server = bridge_module.Bridge, bridge_module.server


@contextlib.contextmanager
def endpoint(name: str, workspace: dict[str, Any] | None = None):
    hub = Hub()
    calls: list[tuple[str, dict[str, Any]]] = []

    class Handler(BaseHTTPRequestHandler):
        def respond(self, data: dict[str, Any], status: int = 200):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/project":
                self.respond({"workspace_url": None, "workspace_only": workspace is not None})
            elif self.path == "/api/workspace" and workspace is not None:
                self.respond(workspace)
            else:
                self.respond({"error": "no such endpoint"}, 404)

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            steps = self.path.strip("/").split("/")
            if steps == ["api", "agent", "sessions"]:
                session = hub.open_session(data["client"], data["skill_digest"], data["mcp_generation"])
                self.respond({**session.info(), "workspace_only": workspace is not None, "project": {"root": name}})
            elif steps[:2] == ["api", "workspace"] and workspace is not None:
                if steps[-1] == "restart":
                    old = next(child for child in workspace["children"] if child["id"] == data["id"])
                    old["status"] = "stopped"
                    replacement = {
                        **old,
                        "id": old["id"] + "-new",
                        "url": workspace["replacement_url"],
                        "status": "running",
                    }
                    workspace["children"].append(replacement)
                    self.respond(replacement)
                elif steps[-1] == "open":
                    child = next(
                        (child for child in workspace["children"] if child["profile_path"] == data["profile"]), None
                    )
                    if child is None:
                        self.respond(
                            {
                                "error": "profile must stay within workspace",
                                "code": "invalid_arguments",
                                "details": {"profile": data["profile"]},
                            },
                            400,
                        )
                    else:
                        self.respond(child)
            elif len(steps) >= 4 and steps[:2] == ["api", "agent"]:
                try:
                    hub.session(steps[2])
                except KeyError:
                    self.respond({"error": "no such session"}, 404)
                    return
                if steps[-1] == "close":
                    hub.close_session(steps[2])
                    self.respond({})
                elif steps[-1] == "heartbeat":
                    self.respond(hub.heartbeat(steps[2]))
                elif len(steps) == 5 and steps[3] == "call":
                    tool = steps[-1]
                    calls.append((tool, data))
                    if tool == "fail":
                        self.respond(
                            {
                                "error": "retained build gone",
                                "code": "build_unavailable",
                                "details": {"build_id": "exact"},
                            },
                            410,
                        )
                    else:
                        self.respond({"text": name, "data": {"endpoint_name": name, "tool": tool, "arguments": data}})
            else:
                self.respond({"error": "no such endpoint"}, 404)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{http.server_port}", hub, calls
        finally:
            http.shutdown()
            thread.join()


def project(id: str, url: str) -> dict[str, Any]:
    return {
        "id": id + "-child",
        "project_id": id,
        "profile_path": id + "/cadiste.json",
        "status": "running",
        "url": url,
    }


def test_project_selection_is_bridge_local_and_endpoint_sessions_are_independent():
    with endpoint("one") as (one, hub_one, _), endpoint("two") as (two, hub_two, _):
        workspace = {"root": "/workspace", "projects": [], "children": [project("one", one), project("two", two)]}
        with endpoint("launcher", workspace) as (launcher, _, _):
            first, second = Bridge(launcher), Bridge(launcher)
            try:
                empty = first.call("client", "list_parts", {})
                assert empty.is_error and empty.structured_content["code"] == "workspace_empty"
                assert "projects" in empty.structured_content["error"]
                listed = first.call("client", "projects", {})
                assert listed.structured_content["workspace"]["root"] == "/workspace"
                first.call("client", "projects", {"action": "open", "profile": "one/cadiste.json"})
                second.call("client", "projects", {"action": "select", "project_id": "two"})
                one_result = first.call("client", "list_parts", {}).structured_content
                two_result = second.call("client", "list_parts", {}).structured_content
                assert one_result["endpoint_name"] == "one"
                assert two_result["endpoint_name"] == "two"
                assert one_result["identity"]["project_id"] == "one"
                assert one_result["identity"]["session_id"] != two_result["identity"]["session_id"]
                original = one_result["identity"]["session_id"]
                first.call("client", "projects", {"action": "select", "project_id": "two"})
                first.call("client", "projects", {"action": "select", "project_id": "one"})
                assert first.call("client", "status", {}).structured_content["identity"]["session_id"] == original
                assert len(hub_one.generations()) == 1
                assert len(hub_two.generations()) == 2
            finally:
                first.close()
                second.close()
            assert hub_one.generations() == [] and hub_two.generations() == []


def test_restart_preserves_project_identity_and_other_bridges_follow_replacement():
    with endpoint("before") as (before, _, _), endpoint("after") as (after, after_hub, _):
        workspace = {
            "root": "/workspace",
            "projects": [],
            "children": [project("stable", before)],
            "replacement_url": after,
        }
        with endpoint("launcher", workspace) as (launcher, _, _):
            first, second = Bridge(launcher), Bridge(launcher)
            try:
                for bridge in (first, second):
                    bridge.call("client", "projects", {"action": "select", "project_id": "stable"})
                restarted = first.call("client", "projects", {"action": "restart"}).structured_content
                assert restarted["selected"]["project_id"] == "stable"
                assert restarted["selected"]["endpoint"] == after
                followed = second.call("client", "status", {}).structured_content
                assert followed["endpoint_name"] == "after"
                assert followed["identity"]["project_id"] == "stable"
                assert len(after_hub.generations()) == 2
            finally:
                first.close()
                second.close()


def test_structured_application_errors_survive_transport_and_sessions_reopen():
    with endpoint("project") as (url, hub, _):
        bridge = Bridge(url)
        try:
            original = bridge.call("client", "status", {}).structured_content["identity"]["session_id"]
            hub.close_session(original)
            reopened = bridge.call("client", "status", {}).structured_content["identity"]["session_id"]
            assert reopened != original
            error = bridge.call("client", "fail", {})
            assert error.is_error
            assert error.structured_content["code"] == "build_unavailable"
            assert error.structured_content["details"] == {"build_id": "exact"}
            assert error.structured_content["identity"]["endpoint"] == url
        finally:
            bridge.close()


def test_transport_errors_are_not_misreported_as_application_errors(monkeypatch):
    def unavailable(*_, **__):
        raise URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    bridge = Bridge("http://127.0.0.1:1")
    with pytest.raises(ToolError, match="transport error"):
        bridge.call("client", "status", {})
    bridge.close()


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("baseline", {"action": "pin", "targets": [{"kind": "part", "name": "Part"}]}),
        ("compare_builds", {"target": {"kind": "part", "name": "Part"}, "baseline": "pin", "views": ["front"]}),
        ("renderer_state", {}),
        ("edit_context", {"requests": [{"part": "Part", "component": "pocket", "expressions": ["wall"]}]}),
        (
            "manufacturing_preflight",
            {"target": {"kind": "part", "name": "Part"}, "group": "plate", "orientation": "flat"},
        ),
        ("show", {"part": "Part", "baseline": "pin"}),
        (
            "verify_targets",
            {"targets": [], "baseline": "pin", "check_sets": ["clearances"], "assertions": [{"kind": "solid"}]},
        ),
        (
            "export_target",
            {"target": {"kind": "part", "name": "Part"}, "group": "plate", "orientation": "flat", "preflight": False},
        ),
        (
            "open_bambu",
            {
                "target": {"kind": "part", "name": "Part"},
                "group": "plate",
                "orientation": "flat",
                "verify_import": True,
            },
        ),
    ],
)
def test_public_mcp_tools_forward_workflow_arguments(tool, arguments, monkeypatch):
    monkeypatch.setattr("cadiste.bridge._client", lambda _: "test-client")
    with endpoint("project") as (url, _, calls):
        bridge = Bridge(url)
        try:
            result = asyncio.run(server(bridge).call_tool(tool, arguments))
            assert not result.is_error
            assert calls[-1][0] == tool
            for name, value in arguments.items():
                assert calls[-1][1][name] == value
        finally:
            bridge.close()


def test_bridge_heartbeat_refreshes_exact_session_and_does_not_reopen_missing_sessions(monkeypatch):
    with endpoint("project") as (url, hub, _):
        bridge = Bridge(url)
        opened = bridge._post(
            "/api/agent/sessions", {"client": "heartbeat", "skill_digest": "skill", "mcp_generation": "generation"}
        )
        bridge._sessions[url] = opened
        session = hub.session(opened["id"])
        session.last_seen -= 80
        assert not session.info()["alive"]
        waits = iter([False, True])
        monkeypatch.setattr(bridge._closed, "wait", lambda _: next(waits))
        bridge._heartbeat()
        assert session.info()["alive"]
        hub.close_session(session.id)
        waits = iter([False, True])
        bridge._heartbeat()
        assert hub.generations() == []
        bridge.close()


def test_workspace_application_failure_keeps_existing_selection_and_error_details():
    with endpoint("project") as (url, _, _):
        workspace = {"root": "/workspace", "projects": [], "children": [project("selected", url)]}
        with endpoint("launcher", workspace) as (launcher, _, _):
            bridge = Bridge(launcher)
            try:
                bridge.call("client", "projects", {"action": "select", "project_id": "selected"})
                failed = bridge.call("client", "projects", {"action": "open", "profile": "../outside.json"})
                assert failed.is_error
                assert failed.structured_content["code"] == "invalid_arguments"
                assert failed.structured_content["details"]["profile"] == "../outside.json"
                assert failed.structured_content["identity"]["project_id"] == "selected"
                assert bridge.call("client", "status", {}).structured_content["endpoint_name"] == "project"
            finally:
                bridge.close()
