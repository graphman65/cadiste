import { useCallback, useEffect, useRef, useState } from "react";

import type { HubRequest, SessionInfo, TabState } from "./types";

const TAB_KEY = "cadiste.tab";
const CHANNEL = "cadiste.tabs";
/** How long a page waits for another one to claim its stored tab id. */
const CLAIM_WAIT = 150;
const PUSH_DELAY = 100;

type TabMessage = { type: "who"; id: string } | { type: "mine"; id: string };

let claimed: Promise<string> | null = null;

/**
 * This page's tab id, kept in sessionStorage so a link survives a reload. A duplicated browser tab copies
 * sessionStorage: a live page already answering for the stored id means this one is the copy and takes a new id.
 * Once settled, the page answers for its id as long as it lives.
 */
export function claimTabId(): Promise<string> {
  claimed ??= new Promise((resolve) => {
    const channel = new BroadcastChannel(CHANNEL);
    let id = window.sessionStorage.getItem(TAB_KEY);
    let taken = false;
    channel.onmessage = (event: MessageEvent<TabMessage>) => {
      if (event.data.type === "mine" && event.data.id === id) taken = true;
    };
    if (id !== null) channel.postMessage({ type: "who", id } satisfies TabMessage);
    window.setTimeout(
      () => {
        if (id === null || taken) {
          id = crypto.randomUUID();
          window.sessionStorage.setItem(TAB_KEY, id);
        }
        const own = id;
        channel.onmessage = (event: MessageEvent<TabMessage>) => {
          if (event.data.type === "who" && event.data.id === own) {
            channel.postMessage({ type: "mine", id: own } satisfies TabMessage);
          }
        };
        resolve(own);
      },
      id === null ? 0 : CLAIM_WAIT,
    );
  });
  return claimed;
}

function post(url: string, body: unknown): Promise<Response> {
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

export interface Hub {
  /** This tab's id; null until it is settled. */
  tab: string | null;
  sessions: SessionInfo[];
  link: (session: string) => void;
  decline: (session: string) => void;
  unlink: (session: string) => void;
}

/**
 * The tab's link to the hub: the sessions it lists, its state pushed as it changes (and on every connection), the
 * calls of linked sessions handed to `handle` and answered with what it resolves to (or the error it throws).
 */
export function useHub(state: TabState, handle: (request: HubRequest) => Promise<unknown>): Hub {
  const [tab, setTab] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const handler = useRef(handle);
  handler.current = handle;
  const stateJson = JSON.stringify(state);
  const latest = useRef(stateJson);
  latest.current = stateJson;

  useEffect(() => {
    let live = true;
    void claimTabId().then((id) => live && setTab(id));
    return () => {
      live = false;
    };
  }, []);

  const pushState = useCallback(
    (id: string, json: string) =>
      fetch(`/api/tabs/${id}/state`, { method: "POST", headers: { "Content-Type": "application/json" }, body: json }),
    [],
  );

  useEffect(() => {
    if (tab === null) return;
    const events = new EventSource(`/api/events?tab=${encodeURIComponent(tab)}`);
    // EventSource reconnects by itself; each connection may be to a hub that has not seen this tab's state yet.
    events.addEventListener("open", () => void pushState(tab, latest.current));
    events.addEventListener("sessions", (event: MessageEvent<string>) => {
      const listed: { sessions: SessionInfo[] } = JSON.parse(event.data);
      setSessions(listed.sessions);
    });
    events.addEventListener("request", (event: MessageEvent<string>) => {
      const request: HubRequest = JSON.parse(event.data);
      // The state goes first: the agent reads it as soon as it has the reply (`select` answers with the selection).
      const reply = (body: { ok: true; result: unknown } | { ok: false; error: string }) =>
        void pushState(tab, latest.current)
          .catch(() => undefined)
          .then(() => post(`/api/tabs/${tab}/reply`, { id: request.id, ...body }));
      handler
        .current(request)
        .then((result) => reply({ ok: true, result }))
        .catch((error: unknown) => reply({ ok: false, error: error instanceof Error ? error.message : String(error) }));
    });
    return () => events.close();
  }, [tab, pushState]);

  useEffect(() => {
    if (tab === null) return;
    const timer = window.setTimeout(() => void pushState(tab, stateJson), PUSH_DELAY);
    return () => window.clearTimeout(timer);
  }, [tab, stateJson, pushState]);

  const link = useCallback((session: string) => tab !== null && void post(`/api/sessions/${session}/link`, { tab }), [tab]);
  const decline = useCallback((session: string) => void post(`/api/sessions/${session}/decline`, {}), []);
  const unlink = useCallback((session: string) => void post(`/api/sessions/${session}/unlink`, {}), []);

  return { tab, sessions, link, decline, unlink };
}
