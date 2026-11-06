import type { AssemblyData, Measurement, Project, TabState, TraceData, TraceError } from "./types";

export class TraceFailure extends Error {
  readonly log: string | undefined;

  constructor(message: string, log?: string) {
    super(message);
    this.log = log;
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
    const failure = (body ?? {}) as Partial<TraceError>;
    throw new TraceFailure(failure.error ?? `${response.status} ${response.statusText}`, failure.log);
  }
  return body as T;
}

async function getResponse<T>(url: string, signal?: AbortSignal): Promise<{ body: T; response: Response }> {
  const response = await fetch(url, { signal });
  return { body: await checked<T>(response), response };
}

/**
 * Which build to ask for: the cached one if still valid; `fresh`, built again even so; `latest`, the one the hub has
 * just made for a harness session.
 */
export type BuildMode = "cached" | "fresh" | "latest";

async function getBuilt<T>(url: string, mode: BuildMode, signal?: AbortSignal): Promise<Built<T>> {
  const query = mode === "cached" ? "" : `${url.includes("?") ? "&" : "?"}${mode}=1`;
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

/** What the server writes a target as: STEP (an assembly's tree kept), STL (one mesh), BREP (OpenCascade's own). */
export type ExportFormat = "step" | "stl" | "brep";

/** The file name a download's `Content-Disposition` gives, the UTF-8 one first. */
function attachmentName(disposition: string | null): string | null {
  const encoded = disposition?.match(/filename\*=UTF-8''([^;]+)/i);
  if (encoded) return decodeURIComponent(encoded[1]);
  return disposition?.match(/filename="([^"]+)"/i)?.[1] ?? null;
}

/**
 * A target built again (not traced) and written as a CAD file; `target` is the viewer's (`{kind: "part", name}`,
 * `{kind: "file", path}`, `{kind: "assembly", name}`); `parts` keeps those of a folder's parts (all of them: none).
 */
export async function fetchExport(
  target: Record<string, string>,
  format: ExportFormat,
  parts?: string[],
  signal?: AbortSignal,
): Promise<{ file: Blob; name: string }> {
  const query = new URLSearchParams({ ...target, format });
  for (const part of parts ?? []) query.append("part", part);
  const response = await fetch(`/api/export?${query}`, { signal });
  if (!response.ok) await checked<never>(response);
  const name = attachmentName(response.headers.get("Content-Disposition")) ?? `model.${format}`;
  return { file: await response.blob(), name };
}

/** Build a STEP file and start Bambu Studio on the server machine; completion means the launcher started. */
export async function openInBambuStudio(target: Record<string, string>, parts?: string[]): Promise<void> {
  const response = await fetch("/api/bambu-studio", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target, parts }),
  });
  await checked<Record<string, never>>(response);
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
