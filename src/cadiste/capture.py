"""Private, managed Chromium rendering using the same served UI as the ordinary viewer."""

from __future__ import annotations

import atexit
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any
from urllib.parse import urlparse

from .hub import Hub

PROTOCOL = "cadiste-renderer-v2"


class CaptureError(Exception):
    pass


class PrivateRenderer:
    """Playwright objects live on one owned worker, never an attached user's browser."""

    def __init__(self, url: str, *, hub: Hub, max_sessions: int = 8):
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise CaptureError("private captures require the bound local Cadiste HTTP URL")
        self.url = url.rstrip("/")
        self.max_sessions = max_sessions
        self.hub = hub
        self._closed = False
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cadiste-renderer")
        self._playwright: Any = None
        self._browser: Any = None
        self._pages: OrderedDict[str, tuple[Any, Any]] = OrderedDict()
        self._page_errors: dict[str, list[dict[str, str]]] = {}
        atexit.register(self.close)

    def call(self, session: str, method: str, params: dict[str, Any], timeout: float = 300) -> Any:
        request = uuid.uuid4().hex
        if self._closed:
            raise CaptureError("private renderer is closed")
        self.hub.track_request("agent-" + session, request, method, params)
        future = self._worker.submit(self._execute, session, request, method, params)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as error:
            raise CaptureError(
                f"private renderer request {request} timed out; query request_outcome before retrying or rebuilding"
            ) from error

    def _execute(self, session: str, request: str, method: str, params: dict[str, Any]) -> Any:
        try:
            if self._browser is None:
                try:
                    from playwright.sync_api import sync_playwright
                except ImportError as error:
                    raise CaptureError(
                        "Private screenshots need Python Playwright: install cadiste[screenshots], "
                        "then run python -m playwright install chromium. No user tab was opened or reused."
                    ) from error
                self._playwright = sync_playwright().start()
                try:
                    self._browser = self._playwright.chromium.launch(
                        headless=True, args=["--enable-webgl", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
                    )
                except Exception as error:
                    self._playwright.stop()
                    self._playwright = None
                    raise CaptureError(
                        f"Managed Chromium could not start: {error}. "
                        "Run python -m playwright install chromium. No linked-tab fallback is used."
                    ) from error
            if session not in self._pages:
                while len(self._pages) >= self.max_sessions:
                    old_session, (old_context, _) = self._pages.popitem(last=False)
                    old_context.close()
                    self._page_errors.pop(old_session, None)
                context = self._browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
                page = context.new_page()
                errors: list[dict[str, str]] = []
                self._page_errors[session] = errors

                def record(kind: str, message: str) -> None:
                    errors.append({"kind": kind, "message": message})
                    del errors[:-32]

                page.on("pageerror", lambda error: record("pageerror", str(error)))
                page.on("console", lambda message: record("console", message.text) if message.type == "error" else None)
                page.on("crash", lambda: record("crash", "Managed Chromium renderer crashed"))
                page.set_default_timeout(300000)
                try:
                    page.goto(self.url + "/?capture=1", wait_until="domcontentloaded")
                    page.wait_for_function("window.cadisteCapture !== undefined", timeout=30000)
                    protocol = page.evaluate("window.cadisteCapture.protocol")
                    if protocol != PROTOCOL:
                        raise CaptureError(
                            f"served UI renderer protocol mismatch: {protocol!r}; "
                            "rebuild the frontend and restart Cadiste"
                        )
                except Exception:
                    context.close()
                    raise
                self._pages[session] = (context, page)
            self._pages.move_to_end(session)
            _, page = self._pages[session]
            result = page.evaluate(
                "request => window.cadisteCapture.call(request)", {"id": request, "method": method, "params": params}
            )
            if isinstance(result, dict):
                result["browser_errors"] = list(self._page_errors.get(session, []))
            self.hub.reply("agent-" + session, request, True, result)
            return result
        except Exception as error:
            self.hub.reply("agent-" + session, request, False, str(error))
            if isinstance(error, CaptureError):
                raise
            diagnostics: dict[str, Any] = {"browser_errors": list(self._page_errors.get(session, []))}
            if session in self._pages:
                try:
                    diagnostics["renderer"] = self._pages[session][1].evaluate(
                        "() => window.cadisteCapture?.inspect?.() ?? {phase: 'inspection_unavailable'}"
                    )
                except Exception as inspection_error:
                    diagnostics["inspection_error"] = str(inspection_error)
            raise CaptureError(f"private renderer request {request}: {error}; diagnostics={diagnostics!r}") from error

    def _close(self) -> None:
        for context, _ in self._pages.values():
            context.close()
        self._pages.clear()
        self._page_errors.clear()
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self.close)
        try:
            self._worker.submit(self._close).result(timeout=30)
        finally:
            self._worker.shutdown(wait=False)
