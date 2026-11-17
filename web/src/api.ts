import type { AssemblyData, Measurement, Project, SimulationCommandResult, SimulationControlsConfig, SimulationFeedback, SimulationScene, SimulationTarget, TabState, TraceData, TraceError } from "./types";
import type { Target } from "./App";

export class TraceFailure extends Error {
  readonly log: string | undefined;
  readonly code: string | undefined;
  readonly details: unknown;

  constructor(message: string, log?: string, code?: string, details?: unknown) {
    super(message);
    this.log = log;
    this.code = code;
    this.details = details;
  }
}

/**
 * A build's JSON; whether the server had it cached (it builds again only when the model's source changed); and the
 * name of the file of its shapes, which measurements are taken on (null: the build wrote none).
 */
export interface Built<T> {
  data: T;
  cached: boolean;
  shapes: string | null;
}

async function checked<T>(response: Response): Promise<T> {
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const failure = (body ?? {}) as Partial<TraceError> & { code?: string; details?: unknown };
    throw new TraceFailure(failure.error ?? `${response.status} ${response.statusText}`, failure.log, failure.code, failure.details);
  }
  return body as T;
}

async function getResponse<T>(url: string, signal?: AbortSignal): Promise<{ body: T; response: Response }> {
  const response = await fetch(url, { signal });
  return { body: await checked<T>(response), response };
}

/** Cached/current-source, explicitly fresh, or one exact retained build generation. */
export type BuildMode = "cached" | "fresh" | { build_id: string; baseline?: string };

async function getBuilt<T>(url: string, mode: BuildMode, signal?: AbortSignal): Promise<Built<T>> {
  const query = mode === "cached" ? "" : typeof mode === "object"
    ? `&build_id=${encodeURIComponent(mode.build_id)}${mode.baseline ? `&baseline=${encodeURIComponent(mode.baseline)}` : ""}` : "&fresh=1";
  const { body, response } = await getResponse<T>(url + query, signal);
  return {
    data: body,
    cached: response.headers.get("X-Cadiste-Cache") === "hit",
    shapes: response.headers.get("X-Cadiste-Shapes"),
  };
}

export async function fetchProject(): Promise<Project> {
  return (await getResponse<Project>("/api/project")).body;
}

export interface BackgroundBuild {
  target: Target;
  status: "queued" | "building" | "ready" | "error";
  build_id?: string;
  error?: string;
  log?: string;
}

export interface BuildsSnapshot {
  fingerprint: string;
  project: Pick<Project, "parts" | "folders" | "assemblies" | "variants" | "default_variant"> | null;
  error: string | null;
  targets: BackgroundBuild[];
}

/** Reconnects automatically; every event is a complete snapshot, not a delta. */
export function listenBuilds(
  target: Target | null,
  receive: (snapshot: BuildsSnapshot) => void,
  connection: (state: "connecting" | "connected" | "disconnected") => void,
  failed: (message: string) => void,
): () => void {
  const query = target === null ? "" : `?${new URLSearchParams({ ...target })}`;
  const source = new EventSource(`/api/builds/events${query}`);
  let previous = "";
  let active = true;
  source.onopen = () => { if (active) connection("connected"); };
  source.onerror = () => { if (active) connection("disconnected"); };
  source.addEventListener("builds", (event) => {
    const data = (event as MessageEvent<string>).data;
    if (!active || data === previous) return;
    try {
      receive(JSON.parse(data) as BuildsSnapshot);
      previous = data;
    } catch (error) {
      failed(`Invalid build update: ${error instanceof Error ? error.message : String(error)}`);
    }
  });
  return () => {
    active = false;
    source.close();
  };
}

/** The part's trace. */
export function fetchTrace(part: string, mode: BuildMode = "cached", signal?: AbortSignal): Promise<Built<TraceData>> {
  return getBuilt<TraceData>(`/api/trace?part=${encodeURIComponent(part)}`, mode, signal);
}

/** An assembly, by name, built (not traced) and tessellated. */
export function fetchAssembly(
  name: string,
  mode: BuildMode = "cached",
  signal?: AbortSignal,
): Promise<Built<AssemblyData>> {
  return getBuilt<AssemblyData>(`/api/assembly?name=${encodeURIComponent(name)}`, mode, signal);
}

/** Every part of one file ("chassis/drive_module"), built (not traced) and viewed together. */
export function fetchFile(path: string, mode: BuildMode = "cached", signal?: AbortSignal): Promise<Built<AssemblyData>> {
  return getBuilt<AssemblyData>(`/api/file?path=${encodeURIComponent(path)}`, mode, signal);
}

/** Exact measurements of the viewer's faces, edges and vertices (`paths`) on a build's shapes. */
export async function fetchMeasurement(shapes: string, paths: string[], signal?: AbortSignal): Promise<Measurement> {
  const response = await fetch("/api/measure", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ shapes, paths }),
    signal,
  });
  return checked<Measurement>(response);
}

/** Target exports: exact STEP assembly, named millimeter 3MF project objects, STL mesh, or exact OpenCascade BREP. */
export type ExportFormat = "step" | "3mf" | "stl" | "brep";

/** The file name a download's `Content-Disposition` gives, the UTF-8 one first. */
function attachmentName(disposition: string | null): string | null {
  const encoded = disposition?.match(/filename\*=UTF-8''([^;]+)/i);
  if (encoded) return decodeURIComponent(encoded[1]);
  return disposition?.match(/filename="([^"]+)"/i)?.[1] ?? null;
}

/**
 * Retained exact geometry written as a CAD file; `target` is the viewer's (`{kind: "part", name}`,
 * `{kind: "file", path}`, `{kind: "assembly", name}`); `parts` keeps named folder parts, while `hidden`
 * excludes exact viewer instance/group paths independently.
 */
export async function fetchExport(
  target: Record<string, string>,
  format: ExportFormat,
  parts?: string[],
  hidden?: string[],
  signal?: AbortSignal,
  options: ManufacturingOptions = {},
): Promise<{ file: Blob; name: string; preflight?: unknown }> {
  const artifact = await postJson<{ url: string; path: string; preflight?: unknown }>("/api/export", {
    target, format, only: parts, hidden, ...options,
  }, signal);
  const response = await fetch(artifact.url, { signal });
  if (!response.ok) await checked<never>(response);
  const name = attachmentName(response.headers.get("Content-Disposition")) ?? artifact.path.split("/").at(-1) ?? `model.${format}`;
  return { file: await response.blob(), name, preflight: artifact.preflight };
}

export interface ManufacturingOptions {
  group?: string;
  orientation?: string;
}

export interface CncjsMachining {
  tool_diameter: number;
  cut_depth: number;
  step_down: number;
  safe_z: number;
  feed_rate: number;
  plunge_rate: number;
  spindle_speed: number;
}

/** Download router profile G-code for manual loading; never connects to a machine. */
export async function fetchCncjsExport(
  target: Record<string, string>,
  machining: CncjsMachining,
  options: ManufacturingOptions & { hidden?: string[] } = {},
): Promise<{ file: Blob; name: string; cutting?: unknown }> {
  const artifact = await postJson<{ url: string; path: string; cutting?: unknown }>("/api/cncjs", {
    target, machining, ...options,
  });
  const response = await fetch(artifact.url);
  if (!response.ok) await checked<never>(response);
  const name = attachmentName(response.headers.get("Content-Disposition")) ?? artifact.path.split("/").at(-1) ?? "model.nc";
  return { file: await response.blob(), name, cutting: artifact.cutting };
}

export interface BambuHandoffReport {
  launched: boolean;
  import_confirmed: boolean;
  preflight?: unknown;
  import_verification?: unknown;
  [key: string]: unknown;
}

/** Geometry-only handoff: export, process launch and isolated import verification are separate outcomes. */
export function openInBambuStudio(
  target: Record<string, string>, parts?: string[], hidden?: string[],
  options: ManufacturingOptions & { verify_import?: boolean } = {},
): Promise<BambuHandoffReport> {
  return postJson<BambuHandoffReport>("/api/bambu-studio", { target, parts, hidden, ...options });
}

export function fetchManufacturingPreflight(
  target: Record<string, string>, options: ManufacturingOptions & { hidden?: string[]; overhang_angle?: number } = {},
  signal?: AbortSignal,
): Promise<Record<string, unknown>> {
  return postJson("/api/manufacturing/preflight", { target, ...options }, signal);
}

export async function postJson<T>(url: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return checked<T>(await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
  }));
}

export interface BaselineEntry {
  id: string;
  name?: string | null;
  targets: { target: Target; build_id: string; source_revision?: string }[];
}

export async function fetchBaselines(): Promise<BaselineEntry[]> {
  return (await getResponse<{ baselines: BaselineEntry[] }>("/api/baselines")).body.baselines;
}

export function pinBaseline(target: Target, build_id: string, name?: string): Promise<BaselineEntry> {
  return postJson("/api/baselines", { action: "pin", targets: [{ ...target, build_id }], name });
}

export function releaseBaseline(id: string): Promise<unknown> {
  return postJson("/api/baselines", { action: "release", id });
}

export async function fetchBaselineScene(baseline: string, target: Target): Promise<Built<TraceData | AssemblyData>> {
  const query = new URLSearchParams({ baseline, ...target });
  const { body, response } = await getResponse<TraceData | AssemblyData>(`/api/baseline-scene?${query}`);
  return { data: body, cached: true, shapes: response.headers.get("X-Cadiste-Shapes") };
}

/** A portable articulated Gazebo model, world, meshes and ROS 2 launch file. */
export async function fetchGazebo(target: Record<string, string>): Promise<{ file: Blob; name: string }> {
  const response = await fetch(`/api/gazebo?${new URLSearchParams(target)}`);
  if (!response.ok) await checked<never>(response);
  return { file: await response.blob(), name: attachmentName(response.headers.get("Content-Disposition")) ?? "cadiste-gazebo.zip" };
}


/** Load an adapter's control metadata without exporting or tessellating meshes. */
export async function fetchSimulationControls(target: SimulationTarget, signal?: AbortSignal): Promise<SimulationControlsConfig> {
  return (await getResponse<SimulationControlsConfig>(`/api/simulation/controls?${new URLSearchParams({ ...target })}`, signal)).body;
}

/** The adapter's one-time tessellated scene and explicit physical-link mapping. */
export async function fetchSimulationScene(target: SimulationTarget, configId: string, signal?: AbortSignal): Promise<SimulationScene> {
  return (await getResponse<SimulationScene>(`/api/simulation/scene?${new URLSearchParams({ ...target, config_id: configId })}`, signal)).body;
}

/** One subscriber lifetime. A broken connection is explicit, never silently called live. */
export function listenSimulationFeedback(
  target: SimulationTarget,
  configId: string,
  receive: (feedback: SimulationFeedback) => void,
  failed: (message: string) => void,
): () => void {
  const source = new EventSource(`/api/simulation/feedback?${new URLSearchParams({ ...target, config_id: configId })}`);
  source.addEventListener("feedback", (event) => {
    try {
      receive(JSON.parse((event as MessageEvent<string>).data) as SimulationFeedback);
    } catch (error) {
      source.close();
      failed(`Invalid simulation feedback: ${error instanceof Error ? error.message : String(error)}`);
    }
  });
  source.onerror = () => {
    source.close();
    failed("Simulation feedback disconnected. Disable and enable Live simulation to reconnect; check the ROS runtime if it persists.");
  };
  return () => source.close();
}

export class SimulationCommandFailure extends TraceFailure {
  readonly result: SimulationCommandResult;

  constructor(message: string, result: SimulationCommandResult) {
    super(message);
    this.result = result;
  }
}

/** Publish explicit setpoints; topics and message types are resolved only on the server. */
export async function sendSimulationCommand(
  target: SimulationTarget,
  configId: string,
  command: { values: Record<string, number> } | { stop_all: true },
): Promise<SimulationCommandResult> {
  const response = await fetch("/api/simulation/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target, config_id: configId, ...command }),
  });
  const body = await response.json().catch(() => null) as (Partial<SimulationCommandResult> & Partial<TraceError>) | null;
  if (!response.ok) {
    throw new SimulationCommandFailure(body?.error ?? `${response.status} ${response.statusText}`, {
      published: body?.published ?? {},
      failed: body?.failed ?? {},
    });
  }
  return body as SimulationCommandResult;
}

/** Capture a standalone Markdown snapshot, with no server-side reference store or lookup. */
export async function createReference(tab: string | null, state: TabState): Promise<{ text: string; label: string }> {
  const response = await fetch("/api/references", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tab, state }),
  });
  return checked<{ text: string; label: string }>(response);
}
