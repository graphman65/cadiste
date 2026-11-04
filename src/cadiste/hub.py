"""Coding-agent sessions (``cadiste mcp``) and the viewer tabs they drive, linked one session to one tab.

A session asks for a tab the first time it needs one: every open tab shows a link request with the session's code,
and the user picks the tab with Link. The link is kept by tab id, which a tab keeps across reloads; a tab gone for
good (closed) is asked for again on the next call that needs one.

Tabs listen on Server-Sent Events (``event_stream``); the server sends them the session list as it changes, and
requests (``call``) that the tab answers with ``reply``. Tabs post their state (what is open and selected) whenever
it changes.
"""

from __future__ import annotations

import itertools
import random
import threading
import uuid
from collections import deque
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

_RECONNECT = 5.0
"""Seconds a linked tab gets to come back (a reload) before it counts as closed."""
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

    def info(self) -> dict[str, Any]:
        return {"id": self.id, "client": self.client, "code": self.code, "tab": self.tab, "requesting": self.requesting}


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


class Hub:
    def __init__(self) -> None:
        self._lock = threading.Condition()
        self._sessions: dict[str, Session] = {}
        self._tabs: dict[str, _Tab] = {}
        self._pending: dict[str, _Pending] = {}
        self._connections = itertools.count(1)

    # ---- sessions (the bridge) ----

    def open_session(self, client: str) -> Session:
        with self._lock:
            used = {session.code for session in self._sessions.values()}
            code = f"{random.choice(_WORDS)}-{random.choice(_ANIMALS)}"
            while code in used:
                code = f"{random.choice(_WORDS)}-{random.choice(_ANIMALS)}"
            session = Session(uuid.uuid4().hex, client or "agent", code)
            self._sessions[session.id] = session
            return session

    def close_session(self, session_id: str) -> None:
        with self._lock:
            if self._sessions.pop(session_id, None) is not None:
                self._broadcast_sessions()

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
            self._lock.notify_all()
        try:
            while True:
                with self._lock:
                    self._lock.wait_for(lambda: tab.outbox or tab.connection != connection, keepalive)
                    if tab.connection != connection:
                        return
                    events = list(tab.outbox)
                    tab.outbox.clear()
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

    def call(self, tab_id: str, method: str, params: dict[str, Any], timeout: float) -> Any:
        """Send a request to a tab and wait for its reply."""
        request = uuid.uuid4().hex
        pending = _Pending(tab_id)
        with self._lock:
            self._pending[request] = pending
            self._tab(tab_id).outbox.append(("request", {"id": request, "method": method, "params": params}))
            self._lock.notify_all()
        try:
            if not pending.done.wait(timeout):
                raise HubError(f"the tab did not answer {method!r} within {timeout:.0f} s (is it still open?)")
        finally:
            with self._lock:
                self._pending.pop(request, None)
        if not pending.ok:
            raise HubError(f"the tab could not {method}: {pending.value}")
        return pending.value

    def reply(self, tab_id: str, request: str, ok: bool, value: Any) -> bool:
        with self._lock:
            pending = self._pending.get(request)
            if pending is None or pending.tab != tab_id:
                return False
        pending.ok, pending.value = ok, value
        pending.done.set()
        return True
