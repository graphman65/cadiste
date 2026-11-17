from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from cadiste.hub import Hub, HubError


class _Stream:
    """A tab's event stream read on a thread, as the server's handler reads it."""

    def __init__(self, hub: Hub, tab: str):
        self.events: list[tuple[str, Any]] = []
        self._stream = hub.event_stream(tab, keepalive=0.05)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        for event in self._stream:
            if event is not None:
                self.events.append(event)

    def wait_for(self, kind: str, timeout: float = 2.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [data for name, data in self.events if name == kind]
            if found:
                return found[-1]
            time.sleep(0.01)
        raise AssertionError(f"no {kind!r} event")

    def ended(self, timeout: float = 2.0) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()


def _in_background(act: Any) -> dict[str, Any]:
    done: dict[str, Any] = {}

    def run() -> None:
        try:
            done["value"] = act()
        except Exception as error:
            done["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    done["thread"] = thread
    return done


def test_a_session_waits_for_the_user_to_link_a_tab_and_keeps_it():
    hub = Hub()
    stream = _Stream(hub, "tab-a")
    session = hub.open_session("claude-code")

    waiting = _in_background(lambda: hub.linked_tab(session.id, timeout=2))
    asked = stream.wait_for("sessions")
    deadline = time.monotonic() + 2
    while not any(s["requesting"] for s in asked["sessions"]) and time.monotonic() < deadline:
        asked = stream.wait_for("sessions")
    assert [s["code"] for s in asked["sessions"] if s["requesting"]] == [session.code]

    hub.link(session.id, "tab-a")
    waiting["thread"].join(2)
    assert waiting["value"] == "tab-a"
    assert hub.linked_tab(session.id, timeout=0) == "tab-a"


def test_a_dismissed_link_request_fails_the_waiting_call():
    hub = Hub()
    _Stream(hub, "tab-a")
    session = hub.open_session("agent")

    waiting = _in_background(lambda: hub.linked_tab(session.id, timeout=2))
    time.sleep(0.1)
    hub.decline(session.id)
    waiting["thread"].join(2)

    assert isinstance(waiting["error"], HubError)
    assert "dismissed" in str(waiting["error"])


def test_without_an_open_tab_a_session_is_told_to_open_one():
    hub = Hub()
    session = hub.open_session("agent")

    with pytest.raises(HubError, match="no cadiste tab is open"):
        hub.linked_tab(session.id, timeout=0.1)


def test_a_call_reaches_the_tab_and_returns_its_reply():
    hub = Hub()
    stream = _Stream(hub, "tab-a")

    calling = _in_background(lambda: hub.call("tab-a", "select", {"node": "o3"}, timeout=2))
    request = stream.wait_for("request")
    assert (request["method"], request["params"]) == ("select", {"node": "o3"})
    assert not hub.reply("tab-b", request["id"], True, {})  # another tab cannot answer it
    assert hub.reply("tab-a", request["id"], True, {"done": 1})
    calling["thread"].join(2)

    assert calling["value"] == {"done": 1}


def test_an_error_reply_fails_the_call_with_the_tabs_message():
    hub = Hub()
    stream = _Stream(hub, "tab-a")

    calling = _in_background(lambda: hub.call("tab-a", "highlight", {"paths": ["x"]}, timeout=2))
    hub.reply("tab-a", stream.wait_for("request")["id"], False, "Unknown viewer paths: x")
    calling["thread"].join(2)

    assert "Unknown viewer paths: x" in str(calling["error"])


def test_a_reloaded_tab_replaces_its_old_stream_and_gets_the_requests():
    hub = Hub()
    old = _Stream(hub, "tab-a")
    old.wait_for("sessions")
    new = _Stream(hub, "tab-a")

    assert old.ended()
    calling = _in_background(lambda: hub.call("tab-a", "view", {}, timeout=2))
    hub.reply("tab-a", new.wait_for("request")["id"], True, {})
    calling["thread"].join(2)
    assert calling["value"] == {}


def test_timeout_keeps_late_reply_and_replays_only_the_same_transaction(tmp_path):
    journal = tmp_path / "outcomes.json"
    hub = Hub(journal)
    params = {"target": {"kind": "part", "name": "A"}, "build_id": "generation-one", "views": ["front"]}
    with pytest.raises(HubError, match=r"request capture-1.*timed out"):
        hub.call("tab-a", "capture", params, timeout=0, request_id="capture-1")
    assert hub.outcome("capture-1")["status"] == "timed_out"
    assert not hub.reply("other-tab", "capture-1", True, {})
    result = {"target": params["target"], "build_id": "generation-one", "images": []}
    assert hub.reply("tab-a", "capture-1", True, result)
    assert not hub.reply("tab-a", "capture-1", True, {"build_id": "generation-two"})
    recovered = Hub(journal)
    assert recovered.outcome("capture-1")["value"] == result
    assert recovered.call("tab-a", "capture", params, timeout=0, request_id="capture-1") == result
    with pytest.raises(HubError, match="different transaction"):
        recovered.call("tab-a", "capture", {**params, "build_id": "generation-two"}, timeout=0, request_id="capture-1")


def test_restart_does_not_replay_inflight_private_capture(tmp_path):
    journal = tmp_path / "outcomes.json"
    hub = Hub(journal)
    params = {"target": {"kind": "part", "name": "A"}, "build_id": "one"}
    hub.track_request("agent-session", "inflight", "capture", params)
    restarted = Hub(journal)
    record = restarted.outcome("inflight")
    assert record["status"] == "interrupted"
    assert "do not blindly rebuild" in record["error"]
    with pytest.raises(HubError, match="interrupted"):
        restarted.call("agent-session", "capture", params, timeout=0, request_id="inflight")


def test_outcome_retention_is_bounded_without_evicting_inflight_requests(tmp_path):
    journal = tmp_path / "outcomes.json"
    hub = Hub(journal, retention=2)
    hub.track_request("private", "still-running", "capture", {"build_id": "one"})
    for request in ("old", "new"):
        hub.track_request("private", request, "view", {})
        assert hub.reply("private", request, True, {"done": request})
    assert hub.outcome("still-running")["status"] == "pending"
    assert hub.outcome("new")["status"] == "ok"
    with pytest.raises(HubError, match="no retained"):
        hub.outcome("old")
    assert len(Hub(journal)._outcomes) == 2


def test_renderer_cannot_complete_capture_with_another_build():
    hub = Hub()
    params = {"target": {"kind": "part", "name": "A"}, "build_id": "one"}
    hub.track_request("private", "capture", "capture", params)
    assert hub.reply("private", "capture", True, {**params, "build_id": "two", "images": []})
    outcome = hub.outcome("capture")
    assert outcome["status"] == "error"
    assert "different target/build" in outcome["value"]


def test_session_liveness_is_heartbeat_evidence_not_registration_or_inspection(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("cadiste.hub.time.time", lambda: now[0])
    hub = Hub()
    first = hub.open_session("same-client", skill_digest="old", mcp_generation="old")
    second = hub.open_session("same-client", skill_digest="new", mcp_generation="new")
    # Dataclass factories are captured at definition time; set timestamps explicitly.
    first.last_seen = second.last_seen = now[0]
    now[0] += 80
    assert all(not session["alive"] for session in hub.generations())
    assert hub.session(first.id).info()["last_seen"] == 1000.0
    refreshed = hub.heartbeat(second.id)
    assert refreshed["alive"] and refreshed["last_seen"] == now[0]
    sessions = {session["id"]: session for session in hub.generations()}
    assert not sessions[first.id]["alive"]
    assert sessions[second.id]["alive"]
    assert sessions[first.id]["code"] != sessions[second.id]["code"]
    assert sessions[first.id]["mcp_generation"] == "old"
    hub.close_session(second.id)
    with pytest.raises(KeyError):
        hub.heartbeat(second.id)
