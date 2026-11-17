import { useEffect, useId, useState } from "react";

type Child = {
  id: string;
  project_id: string;
  root: string;
  profile_path: string;
  name: string;
  status: "starting" | "running" | "failed" | "stopped";
  url: string | null;
  error: string | null;
  logs: string;
};
type Project = {
  profile_path: string;
  profile?: { name: string; root: string; target: string; interpreter: string | null };
  error?: string;
  child?: Child | null;
};
type Workspace = { root: string; projects: Project[]; children: Child[]; error: string | null };
type Browse = {
  path: string;
  parent: string | null;
  entries: { name: string; path: string; kind: "directory" | "profile" }[];
  total: number;
  offset: number;
  next_offset: number | null;
};

async function request<T>(path: string, data?: unknown): Promise<T> {
  const response = await fetch(path, data === undefined ? undefined : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  const text = await response.text();
  let result: unknown;
  try { result = JSON.parse(text); } catch { throw new Error(`Workspace request failed (${response.status}): ${text.slice(0, 300)}`); }
  if (!response.ok) {
    const message = result && typeof result === "object" && "error" in result && typeof result.error === "string"
      ? result.error : `Workspace request failed (${response.status})`;
    throw new Error(message);
  }
  return result as T;
}

/** Explicit project execution, separate from the currently displayed model. */
export function ProjectLauncher({ currentRoot }: { currentRoot?: string }) {
  const id = useId();
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [launcher, setLauncher] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [browse, setBrowse] = useState<Browse | null>(null);
  const [selectedProfile, setSelectedProfile] = useState("");
  const [createName, setCreateName] = useState("My project");
  const [createPath, setCreatePath] = useState("my-project");
  const [createInterpreter, setCreateInterpreter] = useState("");
  const [config, setConfig] = useState({
    path: "cadiste.json", name: "My project", root: ".", interpreter: "", target: "",
    parts: "", args: "[]", assemblies: "{}", simulations: "{}", trace_env: "{}", ros_command: '["ros2"]',
  });
  const workspaceLoaded = workspace !== null;

  const refresh = async () => setWorkspace(await request<Workspace>("/api/workspace"));
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const project = await request<{ workspace_url?: string | null }>("/api/project");
        if (cancelled) return;
        if (project.workspace_url && new URL(project.workspace_url, location.href).origin !== location.origin) {
          setLauncher(project.workspace_url);
          return;
        }
        const value = await request<Workspace>("/api/workspace");
        if (!cancelled) setWorkspace(value);
      } catch (cause) {
        if (!cancelled) setError(String(cause instanceof Error ? cause.message : cause));
      }
    })();
    return () => { cancelled = true; };
  }, []);
  useEffect(() => {
    if (!workspaceLoaded || launcher) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void request<Workspace>("/api/workspace").then((value) => {
        if (!cancelled) setWorkspace(value);
      }).catch((cause: Error) => {
        if (!cancelled) setError(cause.message);
      });
    }, 2500);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [workspaceLoaded, launcher]);

  async function act(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    setNotice("");
    try { await action(); } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }
  async function open(profile: string) {
    const child = await request<Child>("/api/workspace/open", { profile });
    await refresh();
    if (child.status === "failed") throw new Error(child.error || "Project failed to start; inspect its startup output.");
    if (child.status === "running" && child.url) {
      location.assign(child.url);
    }
  }
  async function restart(child: Child) {
    const replacement = await request<Child>("/api/workspace/restart", { id: child.id });
    await refresh();
    if (replacement.status !== "running" || !replacement.url) {
      throw new Error(replacement.error || "Replacement server is not ready. Inspect its startup output.");
    }
    setNotice("Owned project server restarted. Opening its replacement viewer; reconnect MCP sessions if needed.");
    location.assign(replacement.url);
  }
  async function list(path = "", offset = 0) {
    setBrowse(await request<Browse>(`/api/workspace/browse?path=${encodeURIComponent(path)}&offset=${offset}&limit=50`));
  }
  const field = (key: keyof typeof config, label: string, multiline = false) => (
    <label htmlFor={`${id}-${key}`} style={{ display: "grid", gap: 4 }}>
      {label}
      {multiline ? <textarea id={`${id}-${key}`} value={config[key]} rows={2} onChange={(event) => setConfig({ ...config, [key]: event.target.value })} />
        : <input id={`${id}-${key}`} value={config[key]} onChange={(event) => setConfig({ ...config, [key]: event.target.value })} required={["path", "name", "root", "target"].includes(key)} />}
    </label>
  );

  const latestChildren = new Map<string, Child>();
  for (const child of workspace?.children ?? []) latestChildren.set(child.project_id || child.profile_path, child);
  const openSessions = [...latestChildren.values()].filter((child) => child.status === "running" || child.status === "starting");
  const openProfiles = new Set(openSessions.map((child) => child.profile_path));
  const availableProjects = workspace?.projects.filter((project) => !openProfiles.has(project.profile_path)) ?? [];

  const body = <section aria-label="Project workspace" className="project-workspace">
    <header className="workspace-header">
      <h1>Project workspace</h1>
      {!launcher && <button type="button" disabled={busy} onClick={() => void act(refresh)}>Refresh project status</button>}
    </header>
    {launcher ? <p><a href={launcher}>Return to workspace recovery</a> to restart, reopen, create or stop owned project servers. This project tab cannot stop unrelated processes.</p> : <>
      <p className="workspace-root"><code>{workspace?.root ?? "Loading workspace…"}</code></p>
      {currentRoot && <p>Current project: <code>{currentRoot}</code></p>}
      {error && <pre role="alert" className="workspace-error">{error}</pre>}
      {notice && <p role="status">{notice}</p>}
      {workspace?.error && <p role="alert" className="workspace-error">{workspace.error}</p>}
      <div className="workspace-sections" aria-busy={busy}>
        <section aria-labelledby={`${id}-sessions`}>
          <h2 id={`${id}-sessions`}>Open sessions</h2>
          {openSessions.length === 0 && <p className="muted">{workspace ? "No open sessions." : "Loading sessions…"}</p>}
          <ul className="workspace-projects">
            {openSessions.map((child) => {
              const project = workspace?.projects.find((item) => item.profile_path === child.profile_path);
              return <li key={child.project_id || child.profile_path}>
                <strong>{project?.profile?.name || child.name}</strong>
                <code className="workspace-profile-path">{child.profile_path}</code>
                <div className="workspace-project-actions">
                  {child.status === "running" && child.url
                    ? <a className="workspace-open-viewer" href={child.url}>Open viewer</a>
                    : <button className="workspace-open-viewer" disabled>Starting…</button>}
                  <button disabled={busy || child.status === "starting"} onClick={() => void act(() => restart(child))}>Restart and reopen</button>
                  <button disabled={busy} onClick={() => void act(async () => {
                    await request("/api/workspace/stop", { id: child.id });
                    await refresh();
                  })}>Stop server</button>
                </div>
                {project?.error && <p role="alert" className="workspace-error">{project.error}</p>}
                <details className="workspace-output">
                  <summary>Startup output</summary>
                  <pre>{child.logs || child.error || "No startup output."}</pre>
                </details>
              </li>;
            })}
          </ul>
        </section>
        <section aria-labelledby={`${id}-available`}>
          <h2 id={`${id}-available`}>Available projects</h2>
          {availableProjects.length === 0 && <p className="muted">{workspace ? "No other projects found." : "Loading projects…"}</p>}
          <ul className="workspace-projects">
            {availableProjects.map((project) => <li key={project.profile_path}>
              <strong>{project.profile?.name || project.child?.name || project.profile_path}</strong>
              <code className="workspace-profile-path">{project.profile_path}</code>
              <div className="workspace-project-actions">
                <button disabled={busy || Boolean(project.error)} onClick={() => void act(() => open(project.profile_path))}>Open project</button>
                {project.child && <button disabled={busy || Boolean(project.error)} onClick={() => void act(() => restart(project.child!))}>Restart and reopen</button>}
              </div>
              {project.error && <p role="alert" className="workspace-error">{project.error}</p>}
              {project.child?.error && <p role="alert" className="workspace-error">{project.child.error}</p>}
              {project.child && <details className="workspace-output">
                <summary>Startup output</summary>
                <pre>{project.child.logs || project.child.error || "No startup output."}</pre>
              </details>}
            </li>)}
          </ul>
        </section>
        <p role="note" className="workspace-trust"><strong>Trusted local code only.</strong> Opening a project executes its Python on this server with your account’s permissions. Profiles do not sandbox code.</p>
        <details className="workspace-tools">
          <summary>Create or load a project</summary>
          <div className="workspace-sections">
        <form onSubmit={(event) => { event.preventDefault(); void act(async () => {
          const created = await request<Project>("/api/workspace/create", { path: createPath, name: createName, interpreter: createInterpreter || null });
          await refresh();
          await open(created.profile_path);
        }); }}>
          <fieldset disabled={busy} style={{ display: "grid", gap: 8 }}>
            <legend>Create a starter project</legend>
            <label htmlFor={`${id}-create-name`}>Project name</label>
            <input id={`${id}-create-name`} value={createName} onChange={(event) => setCreateName(event.target.value)} required />
            <label htmlFor={`${id}-create-path`}>New directory, relative to workspace root</label>
            <input id={`${id}-create-path`} value={createPath} onChange={(event) => setCreatePath(event.target.value)} required />
            <label htmlFor={`${id}-create-python`}>Python interpreter (optional absolute path; defaults to server Python)</label>
            <input id={`${id}-create-python`} value={createInterpreter} onChange={(event) => setCreateInterpreter(event.target.value)} placeholder="/path/to/.venv/bin/python" />
            <p>Creates model.py with a part catalog and assembly, and cadiste.json. Existing directories and files are never overwritten.</p>
            <button type="submit">Create and open starter</button>
          </fieldset>
        </form>
        <section aria-labelledby={`${id}-existing`}>
          <h2 id={`${id}-existing`}>Load an existing project</h2>
          <form onSubmit={(event) => { event.preventDefault(); void act(async () => {
            const registered = await request<Project>("/api/workspace/register", { profile: selectedProfile });
            await open(registered.profile_path);
          }); }}>
            <fieldset disabled={busy} style={{ display: "grid", gap: 8 }}>
              <legend>Existing profile</legend>
              <label htmlFor={`${id}-profile`}>Profile JSON path within workspace</label>
              <input id={`${id}-profile`} value={selectedProfile} onChange={(event) => setSelectedProfile(event.target.value)} placeholder="existing-project/cadiste.json" required />
              <button type="button" onClick={() => void act(() => list())}>Browse workspace</button>
              <button type="submit">Load and open profile</button>
            </fieldset>
          </form>
          {browse && <nav aria-label="Workspace directories" style={{ margin: "12px 0" }}>
            <p>Browsing <code>{browse.path || "."}</code> ({browse.total} entries)</p>
            {browse.parent !== null && <button disabled={busy} onClick={() => void act(() => list(browse.parent!))}>Parent directory</button>}
            <ul>{browse.entries.map((entry) => <li key={entry.path}><button disabled={busy} onClick={() => {
              if (entry.kind === "directory") void act(() => list(entry.path));
              else setSelectedProfile(entry.path);
            }}>{entry.kind === "directory" ? "Directory: " : "Select profile: "}{entry.name}</button></li>)}</ul>
            {browse.offset > 0 && <button disabled={busy} onClick={() => void act(() => list(browse.path, Math.max(0, browse.offset - 50)))}>Previous entries</button>}
            {browse.next_offset !== null && <button disabled={busy} onClick={() => void act(() => list(browse.path, browse.next_offset!))}>Next entries</button>}
          </nav>}
          <details>
            <summary>Project has no profile? Write explicit configuration</summary>
            <p>Nothing is imported during validation. Saving creates a new JSON file; opening it then executes the configured Python. Named target mappings and environment use JSON objects.</p>
            <form onSubmit={(event) => { event.preventDefault(); void act(async () => {
              const configuration = {
                version: 1, name: config.name, root: config.root, interpreter: config.interpreter || null,
                target: config.target, parts: config.parts || null, args: JSON.parse(config.args),
                assemblies: JSON.parse(config.assemblies), simulations: JSON.parse(config.simulations), trace_env: JSON.parse(config.trace_env),
                ros_command: JSON.parse(config.ros_command),
              };
              const registered = await request<Project>("/api/workspace/register", { path: config.path, configuration });
              setSelectedProfile(registered.profile_path);
              await refresh();
              setNotice("Profile saved and registered. Use Load and open profile to execute it.");
            }); }}>
              <fieldset disabled={busy} style={{ display: "grid", gap: 8 }}>
                <legend>Explicit profile configuration</legend>
                {field("path", "New profile path (relative to workspace root)")}
                {field("name", "Project name")}
                {field("root", "Existing project root (relative to profile directory)")}
                {field("interpreter", "Python interpreter (optional executable path)")}
                {field("target", "Build target: module:callable")}
                {field("parts", "Part catalog: module:attribute (optional)")}
                {field("args", "Build arguments (JSON string array)", true)}
                {field("assemblies", "Assemblies (JSON name → module:callable)", true)}
                {field("simulations", "Simulations (JSON assembly name → module:callable)", true)}
                {field("trace_env", "Trace environment (JSON variable → string)", true)}
                {field("ros_command", "ROS command prefix (JSON argument array, native or Docker)", true)}
                <button type="submit">Validate, save and register profile</button>
              </fieldset>
            </form>
          </details>
        </section>
          </div>
        </details>
      </div>
    </>}
  </section>;

  return currentRoot ? <details className="project-launcher"><summary>Projects</summary>{body}</details> : body;
}
