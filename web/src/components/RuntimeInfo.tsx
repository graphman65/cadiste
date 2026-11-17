import { useEffect, useRef, useState } from "react";

interface Runtime {
  pid: number;
  python: string;
  package: string;
  root: string;
  url: string | null;
  workspace_url?: string | null;
  backend: { running: string; disk: string; matches: boolean };
  frontend: { directory: string; bundle_id: string; source_id: string; stale: boolean; assets: string[] };
  skill: { disk: string | null; mcp: string | null };
  renderer_protocol: string;
  mcp_sessions?: { id: string; code: string; client: string; last_seen: number; alive: boolean; skill_digest?: string | null; mcp_generation?: string | null }[];
  warnings: string[];
}

/** Actual process and served asset generations, deliberately separate from CAD build identity. */
export function RuntimeInfo() {
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [error, setError] = useState<string | null>(null);
  const first = useRef<string | null>(null);
  const [refreshKey, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const refresh = async () => {
      try {
        const response = await fetch("/api/runtime", { signal: controller.signal, cache: "no-store" });
        if (!response.ok) throw new Error(`Runtime diagnostics: HTTP ${response.status}`);
        const value: Runtime = await response.json();
        first.current ??= value.backend.running + ":" + value.pid;
        setRuntime(value);
        setError(null);
      } catch (error) {
        if (!controller.signal.aborted) setError(error instanceof Error ? error.message : String(error));
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [refreshKey]);
  const warnings = [...(runtime?.warnings ?? [])];
  if (error !== null) warnings.push(error);
  if (runtime !== null) {
    if (first.current !== runtime.backend.running + ":" + runtime.pid) warnings.push("Backend process changed since this tab loaded. Reload this tab.");
    const scripts = [...document.querySelectorAll<HTMLScriptElement>("script[src]")].map((script) => new URL(script.src).pathname);
    if (scripts.some((path) => path.startsWith("/assets/") && !runtime.frontend.assets.includes(path))) warnings.push("This tab loaded a different UI bundle from the one now served. Reload this tab.");
    if (runtime.renderer_protocol !== "cadiste-renderer-v2") warnings.push("UI/backend renderer protocol mismatch. Rebuild the UI and restart Cadiste.");
    for (const session of runtime.mcp_sessions ?? []) {
      if (!session.alive) continue;
      const identity = `${session.client} · ${session.code} · session ${session.id}`;
      if (session.skill_digest && session.skill_digest !== runtime.skill.disk) warnings.push(`${identity}: MCP skill is stale; reconnect this MCP session.`);
      if (session.mcp_generation && session.mcp_generation !== runtime.backend.disk) warnings.push(`${identity}: MCP tool code is stale; reconnect this MCP session.`);
      if (session.mcp_generation && session.mcp_generation !== runtime.backend.running) warnings.push(`${identity}: MCP and running backend differ; restart this project from its workspace, then reconnect this MCP session.`);
    }
  }
  return <details className={warnings.length > 0 ? "runtime-info stale" : "runtime-info"}>
    <summary>{warnings.length > 0 ? "Runtime mismatch" : "Runtime"}</summary>
    <div className="runtime-info-panel">
      {warnings.map((warning, index) => <p role="alert" key={index}>{warning}</p>)}
      <div className="toolbar">
        <button onClick={() => setRefresh((value) => value + 1)}>Refresh diagnostics</button>
        <button onClick={() => location.reload()}>Reload this tab</button>
        <a href={runtime?.workspace_url || "/"}>Open workspace recovery</a>
      </div>
      {warnings.length > 0 && <p>Backend mismatch: restart and reopen the owned project from Projects. MCP mismatch: reconnect only the identified session in your coding client. An unmanaged server must be restarted from its owning terminal.</p>}
      {runtime === null ? <p>{error ?? "Loading runtime…"}</p> : <dl>
        <dt>Process</dt><dd>{runtime.pid} · {runtime.python}</dd>
        <dt>Package</dt><dd>{runtime.package}</dd>
        <dt>Project</dt><dd>{runtime.root}</dd>
        <dt>Backend running / disk</dt><dd>{runtime.backend.running.slice(0, 12)} / {runtime.backend.disk.slice(0, 12)}</dd>
        <dt>Served UI / source</dt><dd>{runtime.frontend.bundle_id.slice(0, 12)} / {runtime.frontend.source_id.slice(0, 12)}</dd>
        <dt>UI directory</dt><dd>{runtime.frontend.directory}</dd>
        <dt>MCP skill / disk</dt><dd>{runtime.skill.mcp?.slice(0, 12) ?? "not reported"} / {runtime.skill.disk?.slice(0, 12) ?? "unavailable"}</dd>
      </dl>}
      {(runtime?.mcp_sessions?.length ?? 0) > 0 && <details><summary>MCP session identities</summary>
        <ul>{runtime!.mcp_sessions!.map((session) => <li key={session.id}>
          <strong>{session.client}</strong> · {session.code} · <code>{session.id}</code>
          {" · "}{session.alive ? "live heartbeat" : "inactive registration"}
          {" · last seen "}{new Date(session.last_seen * 1000).toLocaleString()}
        </li>)}</ul>
      </details>}
    </div>
  </details>;
}
