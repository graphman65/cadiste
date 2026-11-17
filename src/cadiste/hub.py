"""Coding-agent sessions, explicitly linked tabs, and recoverable renderer transactions.

A session asks for a tab the first time it needs one: every open tab shows a link request with the session's code,
and the user picks the tab with Link. The link is kept by tab id, which a tab keeps across reloads; a tab gone for
good (closed) is asked for again on the next call that needs one.

Tabs listen on Server-Sent Events (``event_stream``); the server sends them the session list as it changes, and
requests (``call``) that the tab answers with ``reply``. Tabs post their state (what is open and selected) whenever
it changes.
"""

from __future__ import annotations

import itertools
import json
import random
import threading
import time
import uuid
from collections import deque
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_RECONNECT = 5.0
"""Seconds a linked tab gets to come back (a reload) before it counts as closed."""
_SESSION_TTL = 75.0
"""A bridge heartbeats every 20 seconds; old registrations are not live clients."""
_WORDS = ("amber", "brave", "brisk", "calm", "cedar", "coral", "crisp", "dusky", "eager", "ember", "fern", "frosty")
_WORDS += ("golden", "hazel", "ivory", "jade", "keen", "lunar", "maple", "misty", "noble", "olive", "pearl", "quiet")
_WORDS += ("rapid", "rosy", "rusty", "sage", "silver", "snowy", "solar", "spruce", "sunny", "swift", "teal", "vivid")
_ANIMALS = ("badger", "bear", "beaver", "bison", "crane", "crow", "deer", "eagle", "egret", "falcon", "ferret", "finch")
_ANIMALS += ("fox", "gecko", "heron", "ibis", "koala", "lark", "lemur", "lynx", "marten", "moose", "newt", "orca")
_ANIMALS += ("otter", "owl", "panda", "puffin", "quail", "raven", "robin", "seal", "stork", "swan", "tapir", "wren")


class HubError(Exception):
    """A session or tab call that cannot go through; the message is for the agent."""


@dataclass
class Session:
    id: str
    client: str
    code: str
    tab: str | None = None
    requesting: bool = False
    declined: bool = False
    skill_digest: str | None = None
    mcp_generation: str | None = None
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def info(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "client": self.client,
            "code": self.code,
            "tab": self.tab,
            "requesting": self.requesting,
            "skill_digest": self.skill_digest,
            "mcp_generation": self.mcp_generation,
            "created": self.created,
            "last_seen": self.last_seen,
            "alive": time.time() - self.last_seen <= _SESSION_TTL,
        }


@dataclass
class _Tab:
    id: str
    state: dict[str, Any] = field(default_factory=dict)
    connection: int | None = None
    """The live event stream; a newer one replaces it (a reload reconnects before the old socket is seen dead)."""
    outbox: deque[tuple[str, Any]] = field(default_factory=deque)
    """Events for the stream, kept while it reconnects."""


@dataclass
class _Pending:
    tab: str
    done: threading.Event = field(default_factory=threading.Event)
    ok: bool = False
    value: Any = None
    method: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    created: float = field(default_factory=time.time)


class Hub:
    def __init__(self, outcomes: Path | None = None, *, retention: int = 256) -> None:
        self._lock = threading.Condition()
        self._sessions: dict[str, Session] = {}
        self._tabs: dict[str, _Tab] = {}
        self._pending: dict[str, _Pending] = {}
        self._connections = itertools.count(1)
        self._outcomes_path = outcomes
        self._retention = max(1, retention)
        self._outcomes: dict[str, dict[str, Any]] = {}
        if outcomes is not None and outcomes.exists():
            try:
                saved = json.loads(outcomes.read_text())
                for item in saved[-self._retention :]:
                    if item["status"] == "pending":
                        item = {
                            **item,
                            "status": "interrupted",
                            "error": "server restarted; outcome unknown; do not blindly rebuild",
                        }
                    self._outcomes[item["id"]] = item
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def _persist(self) -> None:
        while len(self._outcomes) > self._retention:
            removable = next((key for key, value in self._outcomes.items() if value["status"] != "pending"), None)
            if removable is None:
                break
            del self._outcomes[removable]
        if self._outcomes_path is not None:
            self._outcomes_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._outcomes_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(list(self._outcomes.values()), ensure_ascii=False))
            temporary.replace(self._outcomes_path)

    def outcome(self, request: str) -> dict[str, Any]:
        """Read a retained request without resending it, including replies arriving after timeout."""
        with self._lock:
            if request not in self._outcomes:
                raise HubError(f"no retained renderer request {request!r}")
            return dict(self._outcomes[request])

    def generations(self) -> list[dict[str, Any]]:
        with self._lock:
            return [session.info() for session in self._sessions.values()]

    def track_request(self, tab: str, request: str, method: str, params: dict[str, Any]) -> None:
        """Register a private renderer transaction in the same durable recovery journal."""
        with self._lock:
            if request in self._outcomes:
                raise HubError(f"request {request} already exists")
            if sum(value["status"] == "pending" for value in self._outcomes.values()) >= self._retention:
                raise HubError("renderer request retention is full")
            self._outcomes[request] = {
                "id": request,
                "tab": tab,
                "method": method,
                "params": params,
                "status": "pending",
                "created": time.time(),
            }
            self._persist()

    # ---- sessions (the bridge) ----

    def open_session(self, client: str, skill_digest: str | None = None, mcp_generation: str | None = None) -> Session:
        with self._lock:
            used = {session.code for session in self._sessions.values()}
            code = f"{random.choice(_WORDS)}-{random.choice(_ANIMALS)}"
            while code in used:
                code = f"{random.choice(_WORDS)}-{random.choice(_ANIMALS)}"
            session = Session(
                uuid.uuid4().hex, client or "agent", code, skill_digest=skill_digest, mcp_generation=mcp_generation
            )
            self._sessions[session.id] = session
            return session

    def close_session(self, session_id: str) -> None:
        with self._lock:
            if self._sessions.pop(session_id, None) is not None:
                self._broadcast_sessions()

    def heartbeat(self, session_id: str) -> dict[str, Any]:
        """Refresh exactly one registered bridge; inspection never refreshes liveness."""
        with self._lock:
            session = self.session(session_id)
            was_alive = session.info()["alive"]
            session.last_seen = time.time()
            if not was_alive:
                self._broadcast_sessions()
            return session.info()

    def session(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            return session

    def link(self, session_id: str, tab: str) -> None:
        with self._lock:
            session = self.session(session_id)
            session.tab, session.requesting, session.declined = tab, False, False
            self._broadcast_sessions()
            self._lock.notify_all()

    def decline(self, session_id: str) -> None:
        with self._lock:
            session = self.session(session_id)
            if session.requesting:
                session.requesting, session.declined = False, True
                self._broadcast_sessions()
                self._lock.notify_all()

    def unlink(self, session_id: str) -> None:
        with self._lock:
            session = self.session(session_id)
            session.tab = None
            self._broadcast_sessions()

    def _live(self, tab_id: str | None) -> bool:
        tab = self._tabs.get(tab_id) if tab_id is not None else None
        return tab is not None and tab.connection is not None

    def linked_tab(self, session_id: str, timeout: float) -> str:
        """The session's tab; when it has none (or its tab was closed), ask every tab for a link and wait up to
        ``timeout`` for the user to give one."""
        with self._lock:
            session = self.session(session_id)
            if session.tab is not None and not self._live(session.tab):
                self._lock.wait_for(lambda: self._live(session.tab), _RECONNECT)
                if not self._live(session.tab):
                    session.tab = None
            if session.tab is not None:
                return session.tab
            if not any(tab.connection is not None for tab in self._tabs.values()):
                raise HubError(
                    "no cadiste tab is open: ask the user to open the viewer (the URL `cadiste serve` prints), "
                    "then call again"
                )
            session.declined = False
            if not session.requesting:
                session.requesting = True
                self._broadcast_sessions()
            self._lock.wait_for(lambda: session.tab is not None or session.declined, timeout)
            if session.tab is not None:
                return session.tab
            if session.declined:
                raise HubError("the user dismissed the link request in the viewer")
            raise HubError(
                f"waiting for the user to link a tab: every cadiste tab shows a link request with the code "
                f"{session.code}. Ask the user to click 'Link this tab' in the tab to use, then call again."
            )

    def tab_state(self, tab_id: str) -> dict[str, Any]:
        with self._lock:
            tab = self._tabs.get(tab_id)
            return dict(tab.state) if tab is not None else {}

    # ---- tabs (the browser) ----

    def _tab(self, tab_id: str) -> _Tab:
        tab = self._tabs.get(tab_id)
        if tab is None:
            tab = self._tabs[tab_id] = _Tab(tab_id)
        return tab

    def _broadcast_sessions(self) -> None:
        sessions = [session.info() for session in self._sessions.values()]
        for tab in self._tabs.values():
            # Only the latest list matters: drop one still waiting to go out.
            kept = [item for item in tab.outbox if item[0] != "sessions"]
            tab.outbox = deque([*kept, ("sessions", {"sessions": sessions})])
        self._lock.notify_all()

    def event_stream(self, tab_id: str, keepalive: float = 15.0) -> Generator[tuple[str, Any] | None]:
        """The events for a tab's stream, None when ``keepalive`` passes without any; ends when a newer stream for
        the same tab replaces it. Close the iterator when the client goes away."""
        with self._lock:
            tab = self._tab(tab_id)
            connection = tab.connection = next(self._connections)
            tab.outbox = deque(item for item in tab.outbox if item[0] != "sessions")
            tab.outbox.appendleft(("sessions", {"sessions": [s.info() for s in self._sessions.values()]}))
            reported_liveness = tuple((session.id, session.info()["alive"]) for session in self._sessions.values())
            self._lock.notify_all()
        try:
            while True:
                with self._lock:
                    self._lock.wait_for(lambda: tab.outbox or tab.connection != connection, keepalive)
                    if tab.connection != connection:
                        return
                    events = list(tab.outbox)
                    tab.outbox.clear()
                    liveness = tuple((session.id, session.info()["alive"]) for session in self._sessions.values())
                    if liveness != reported_liveness:
                        if not any(kind == "sessions" for kind, _ in events):
                            events.append(("sessions", {"sessions": [s.info() for s in self._sessions.values()]}))
                        reported_liveness = liveness
                if not events:
                    yield None
                yield from events
        finally:
            with self._lock:
                if tab.connection == connection:
                    tab.connection = None
                    self._lock.notify_all()

    def set_state(self, tab_id: str, state: dict[str, Any]) -> None:
        with self._lock:
            self._tab(tab_id).state = state
            self._lock.notify_all()

    def call(
        self, tab_id: str, method: str, params: dict[str, Any], timeout: float, *, request_id: str | None = None
    ) -> Any:
        """Send once; a timeout retains the request so a late reply can be recovered by id."""
        request = request_id or uuid.uuid4().hex
        with self._lock:
            previous = self._outcomes.get(request)
            if previous is not None:
                if (previous["tab"], previous["method"], previous["params"]) != (tab_id, method, params):
                    raise HubError(f"request id {request} was already used for a different transaction")
                if previous["status"] == "ok":
                    return previous["value"]
                raise HubError(f"request {request}: {previous['status']}; query request_outcome before retrying")
            if sum(item["status"] == "pending" for item in self._outcomes.values()) >= self._retention:
                raise HubError("renderer request retention is full; recover outstanding requests before sending more")
            pending = _Pending(tab_id, method=method, params=params)
            self._pending[request] = pending
            self._outcomes[request] = {
                "id": request,
                "tab": tab_id,
                "method": method,
                "params": params,
                "status": "pending",
                "created": pending.created,
            }
            self._persist()
            self._tab(tab_id).outbox.append(("request", {"id": request, "method": method, "params": params}))
            self._lock.notify_all()
        if not pending.done.wait(timeout):
            with self._lock:
                if not pending.done.is_set():
                    self._pending.pop(request, None)
                    self._outcomes[request]["status"] = "timed_out"
                    self._persist()
                    raise HubError(
                        f"renderer request {request} ({method}) timed out after {timeout:.0f} s; "
                        "query request_outcome(request_id=...) before retrying or rebuilding"
                    )
        if not pending.ok:
            raise HubError(f"the tab could not {method} (request {request}): {pending.value}")
        return pending.value

    def reply(self, tab_id: str, request: str, ok: bool, value: Any) -> bool:
        with self._lock:
            record = self._outcomes.get(request)
            if record is None or record["tab"] != tab_id or record["status"] not in ("pending", "timed_out"):
                return False
            expected = record["params"].get("expected", record["params"])
            if (
                ok
                and record["method"] in ("open", "capture", "screenshot")
                and expected.get("build_id")
                and (
                    not isinstance(value, dict)
                    or any(value.get(key) != expected.get(key) for key in ("target", "build_id"))
                )
            ):
                ok, value = False, "renderer returned a different target/build; transaction discarded"
            record.update(status="ok" if ok else "error", value=value, completed=time.time())
            pending = self._pending.pop(request, None)
            if pending is not None:
                pending.ok, pending.value = ok, value
            self._persist()
            if pending is not None:
                pending.done.set()
            return True
