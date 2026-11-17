import { useEffect, useRef, useState } from "react";

import type { Target } from "../App";
import { type CncjsMachining, type ExportFormat, fetchCncjsExport, fetchExport, fetchGazebo, fetchManufacturingPreflight, openInBambuStudio, TraceFailure } from "../api";
import { Icon } from "./Icon";

const FORMATS: { format: ExportFormat; label: string; hint: string }[] = [
  { format: "step", label: "STEP", hint: "CAD solids" },
  { format: "3mf", label: "3MF", hint: "Print meshes" },
  { format: "stl", label: "STL", hint: "Triangle mesh" },
  { format: "brep", label: "BREP", hint: "OpenCascade solids" },
];

const CUTTING_FIELDS: { key: keyof CncjsMachining; label: string }[] = [
  { key: "tool_diameter", label: "Cutter diameter (mm)" },
  { key: "cut_depth", label: "Cut depth (mm, positive down)" },
  { key: "step_down", label: "Step-down (mm)" },
  { key: "safe_z", label: "Clearance Z (mm above stock)" },
  { key: "feed_rate", label: "Feed (mm/min)" },
  { key: "plunge_rate", label: "Plunge (mm/min)" },
  { key: "spindle_speed", label: "Spindle (integer RPM)" },
];

interface ManufacturingReport {
  build_id?: string;
  source_revision?: string;
  object_count: number;
  solid_count: number;
  bounds_mm: { dimensions: number[] };
  objects: {
    path: string;
    mesh: { closed_oriented_manifold: boolean; overhang: { area_mm2: number }; bridge_candidates: { max_xy_patch_extent_mm: number } };
    bed_placement: { on_z0: boolean; below_z0: boolean; within_registered_bed: boolean | null };
  }[];
  warnings: string[];
  limitations: string[];
}

interface ImportReport {
  status?: string;
  import_confirmed?: boolean;
  object_counts_match?: boolean;
  dimensions_match?: boolean;
  placement_match?: boolean;
  reason?: string;
  action?: string;
  log?: string;
}

type State =
  | { status: "idle" }
  | { status: "pending"; action: string }
  | { status: "complete"; action: string; report?: ManufacturingReport; launched?: boolean; imported?: ImportReport }
  | { status: "error"; error: string; log?: string };

export interface ManufacturingConfiguration {
  orientations?: Record<string, unknown>;
  groups?: Record<string, unknown>;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error(`Invalid ${label} report`);
  return Object.fromEntries(Object.entries(value));
}

function finite(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error("Invalid numeric manufacturing evidence");
  return value;
}

function text(value: unknown): string {
  if (typeof value !== "string") throw new Error("Invalid text manufacturing evidence");
  return value;
}

function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error("Invalid boolean manufacturing evidence");
  return value;
}

function texts(value: unknown): string[] {
  if (!Array.isArray(value)) throw new Error("Invalid manufacturing evidence list");
  return value.map(text);
}

/** Validate the displayed network evidence once, rather than asserting an unchecked response shape. */
function manufacturingReport(value: unknown): ManufacturingReport | undefined {
  if (value === undefined || value === null) return undefined;
  const source = record(value, "manufacturing");
  const bounds = record(source.bounds_mm, "bounds");
  if (!Array.isArray(bounds.dimensions) || bounds.dimensions.length !== 3 || !Array.isArray(source.objects)) throw new Error("Invalid manufacturing objects or dimensions");
  return {
    build_id: source.build_id == null ? undefined : text(source.build_id),
    source_revision: source.source_revision == null ? undefined : text(source.source_revision),
    object_count: finite(source.object_count), solid_count: finite(source.solid_count),
    bounds_mm: { dimensions: bounds.dimensions.map(finite) },
    warnings: texts(source.warnings), limitations: texts(source.limitations),
    objects: source.objects.map((value) => {
      const object = record(value, "object");
      const mesh = record(object.mesh, "mesh");
      const overhang = record(mesh.overhang, "overhang");
      const bridge = record(mesh.bridge_candidates, "bridge");
      const bed = record(object.bed_placement, "bed placement");
      return {
        path: text(object.path),
        mesh: { closed_oriented_manifold: boolean(mesh.closed_oriented_manifold), overhang: { area_mm2: finite(overhang.area_mm2) },
          bridge_candidates: { max_xy_patch_extent_mm: finite(bridge.max_xy_patch_extent_mm) } },
        bed_placement: { on_z0: boolean(bed.on_z0), below_z0: boolean(bed.below_z0),
          within_registered_bed: bed.within_registered_bed === null ? null : boolean(bed.within_registered_bed) },
      };
    }),
  };
}

function importReport(value: unknown): ImportReport | undefined {
  if (value === undefined || value === null) return undefined;
  const source = record(value, "native import");
  return {
    status: source.status === undefined ? undefined : text(source.status),
    import_confirmed: source.import_confirmed === undefined ? undefined : boolean(source.import_confirmed),
    object_counts_match: source.object_counts_match === undefined ? undefined : boolean(source.object_counts_match),
    dimensions_match: source.dimensions_match === undefined ? undefined : boolean(source.dimensions_match),
    placement_match: source.placement_match === undefined ? undefined : boolean(source.placement_match),
    reason: source.reason === undefined ? undefined : text(source.reason),
    action: source.action === undefined ? undefined : text(source.action),
    log: source.log === undefined ? undefined : text(source.log),
  };
}

function cuttingReport(value: unknown) {
  const source = record(value, "cutting");
  const partial = boolean(source.partial_export);
  const pocketCount = finite(source.skipped_pocket_count);
  const holeCount = finite(source.skipped_hole_count);
  if (!Number.isInteger(pocketCount) || pocketCount < 0 || !Number.isInteger(holeCount) || holeCount < 0 ||
      partial !== (pocketCount + holeCount > 0) ||
      !Array.isArray(source.skipped_pockets) || source.skipped_pockets.length !== pocketCount ||
      !Array.isArray(source.skipped_holes) || source.skipped_holes.length !== holeCount) {
    throw new Error("Invalid skipped pocket or hole report");
  }
  for (const value of source.skipped_pockets) {
    const pocket = record(value, "skipped pocket");
    const side = text(pocket.side);
    const depth = finite(pocket.depth_mm);
    const volume = finite(pocket.volume_mm3);
    const bounds = record(pocket.bounds_mm, "skipped pocket bounds");
    if (!["top", "bottom", "both", "internal"].includes(side) || depth <= 0 || volume <= 0 ||
        !Array.isArray(bounds.min) || bounds.min.length !== 3 ||
        !Array.isArray(bounds.max) || bounds.max.length !== 3) {
      throw new Error("Invalid skipped pocket geometry");
    }
    const min = bounds.min.map(finite);
    const max = bounds.max.map(finite);
    if (min.some((value, axis) => value > max[axis])) throw new Error("Invalid skipped pocket bounds");
  }
  const reasons = source.skipped_holes.map((value) => {
    const hole = record(value, "skipped hole");
    const reason = text(hole.reason);
    const bounds = record(hole.bounds_mm, "skipped hole bounds");
    if (!reason.trim() || !Array.isArray(bounds.min) || bounds.min.length !== 3 ||
        !Array.isArray(bounds.max) || bounds.max.length !== 3) {
      throw new Error("Invalid skipped hole geometry or reason");
    }
    const min = bounds.min.map(finite);
    const max = bounds.max.map(finite);
    if (min.some((value, axis) => value > max[axis])) throw new Error("Invalid skipped hole bounds");
    return reason;
  });
  return { partial, pocketCount, holeCount, reasons: [...new Set(reasons)] };
}

/** Hands a file to the browser as a download. */
function download(file: Blob, name: string) {
  const url = URL.createObjectURL(file);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  // Revoked once the browser has had time to start the download from it.
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** Visibility scope of the current composite scene; excludes paths, not part names. */
export interface Shown {
  hidden: string[];
  visible: number;
  of: number;
  unit: "parts" | "instances";
}

/**
 * The top bar's Export button: CAD downloads and server-local application handoffs.
 * A configured assembly also exports an articulated Gazebo/ROS 2 bundle.
 */
export function ExportMenu({ target, title, shown, ready, simulation, manufacturing }: {
  target: Target | null;
  title: string | undefined;
  shown: Shown | null;
  ready: boolean;
  simulation: boolean;
  manufacturing?: ManufacturingConfiguration;
}) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<State>({ status: "idle" });
  const [group, setGroup] = useState("");
  const [orientation, setOrientation] = useState("");
  const [verifyImport, setVerifyImport] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const [cuttingValues, setCuttingValues] = useState<Partial<Record<keyof CncjsMachining, string>>>({});
  const root = useRef<HTMLDivElement>(null);
  const request = useRef(0);
  const targetKey = JSON.stringify(target);

  useEffect(() => {
    request.current += 1;
    setState({ status: "idle" });
    setGroup("");
    setOrientation("");
    setShowDetails(false);
  }, [targetKey]);

  useEffect(() => {
    if (!open) return;
    const away = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  const pending = state.status === "pending";
  const empty = shown?.visible === 0;
  const cadDisabled = !ready || empty || pending;
  const options = { group: group || undefined, orientation: orientation || undefined };
  const cuttingValid = CUTTING_FIELDS.every(({ key }) => {
    const value = Number(cuttingValues[key]);
    return Number.isFinite(value) && value > 0 && (key !== "spindle_speed" || Number.isInteger(value));
  });
  const begin = (action: string) => {
    const id = ++request.current;
    setState({ status: "pending", action });
    return id;
  };
  const fail = (id: number, error: unknown) => {
    if (id !== request.current) return;
    const log = error instanceof TraceFailure ? error.log : undefined;
    setState({ status: "error", error: error instanceof Error ? error.message : String(error), log });
    setOpen(true);
  };

  const start = async (format: ExportFormat) => {
    if (target === null || cadDisabled) return;
    const id = begin(`Exporting ${format.toUpperCase()}…`);
    try {
      const result = await fetchExport(target, format, undefined, shown?.hidden, undefined, options);
      if (id !== request.current) return;
      download(result.file, result.name);
      const report = manufacturingReport("preflight" in result ? result.preflight : undefined);
      setState({ status: "complete", action: `${format.toUpperCase()} download started.`, report });
    } catch (error) { fail(id, error); }
  };

  const preflight = async () => {
    if (target === null || cadDisabled) return;
    const id = begin("Checking retained manufacturing geometry…");
    try {
      const result = await fetchManufacturingPreflight(target, { ...options, hidden: shown?.hidden });
      const report = manufacturingReport(result);
      if (!report) throw new Error("Manufacturing preflight returned no report");
      if (id === request.current) setState({ status: "complete", action: "Manufacturing preflight complete.", report });
    } catch (error) { fail(id, error); }
  };

  const launch = async () => {
    if (target === null || cadDisabled) return;
    const id = begin(verifyImport ? "Exporting, launching and checking native import…" : "Exporting and launching Bambu Studio…");
    try {
      const result = await openInBambuStudio(target, undefined, shown?.hidden, { ...options, verify_import: verifyImport });
      if (id !== request.current) return;
      const handoff = record(result, "Bambu handoff");
      setState({ status: "complete", action: "Geometry-only 3MF exported.",
        launched: handoff.launched === undefined ? undefined : boolean(handoff.launched),
        report: manufacturingReport(handoff.preflight), imported: importReport(handoff.import_verification) });
    } catch (error) { fail(id, error); }
  };

  const cncjs = async () => {
    if (target === null || cadDisabled || !cuttingValid) return;
    const machining: CncjsMachining = {
      tool_diameter: Number(cuttingValues.tool_diameter),
      cut_depth: Number(cuttingValues.cut_depth),
      step_down: Number(cuttingValues.step_down),
      safe_z: Number(cuttingValues.safe_z),
      feed_rate: Number(cuttingValues.feed_rate),
      plunge_rate: Number(cuttingValues.plunge_rate),
      spindle_speed: Number(cuttingValues.spindle_speed),
    };
    const id = begin("Generating CNC router G-code…");
    try {
      const result = await fetchCncjsExport(target, machining, { ...options, hidden: shown?.hidden });
      if (id !== request.current) return;
      const cutting = cuttingReport(result.cutting);
      download(result.file, result.name);
      setState({ status: "complete", action: cutting.partial
        ? `Incomplete profile-only G-code downloaded; ${cutting.pocketCount} pockets and ${cutting.holeCount} holes skipped (not machined). ${cutting.reasons.length ? `Hole omissions: ${cutting.reasons.join("; ")}. ` : ""}Load manually in CNCjs; no machine started.`
        : "Supported profile G-code downloaded. Load manually in CNCjs; no machine started." });
    } catch (error) { fail(id, error); }
  };

  const gazebo = async () => {
    if (target === null || pending || !simulation) return;
    const id = begin("Exporting Gazebo…");
    try {
      const { file, name } = await fetchGazebo(target);
      if (id !== request.current) return;
      download(file, name);
      setState({ status: "complete", action: "Simulation bundle exported; browser download started." });
    } catch (error) { fail(id, error); }
  };
  const report = state.status === "complete" ? state.report : undefined;
  const groups = Object.keys(manufacturing?.groups ?? {});
  const orientations = Object.keys(manufacturing?.orientations ?? {});
  const hasDetails = state.status === "complete" || state.status === "error";
  return (
    <div className="export" ref={root}>
      <button
        disabled={target === null}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((shown) => !shown)}
        title={title ? `Export ${title} or open in an external application` : "Export what is open or open in an external application"}
      >
        {pending ? <span className="spinner" /> : <Icon name="download" />}Export
      </button>
      {open && (
        <div className="export-menu" role="dialog" aria-label="Export">
          <div className="export-title">
            <strong>{title}</strong>
            {shown && <span>{shown.visible} of {shown.of} {shown.unit} shown</span>}
          </div>
          {(groups.length > 0 || orientations.length > 0) && (
            <div className="export-options" role="group" aria-label="Manufacturing options">
              {groups.length > 0 && <label>Group
                <select aria-label="Export group" value={group} disabled={pending} onChange={(event) => { setGroup(event.target.value); setState({ status: "idle" }); }}>
                  <option value="">All shown objects</option>
                  {groups.map((name) => <option key={name} value={name}>{name}</option>)}
                </select>
              </label>}
              {orientations.length > 0 && <label>Orientation
                <select aria-label="Export orientation" value={orientation} disabled={pending} onChange={(event) => { setOrientation(event.target.value); setState({ status: "idle" }); }}>
                  <option value="">Per-part / model pose</option>
                  {orientations.map((name) => <option key={name} value={name}>{name}</option>)}
                </select>
              </label>}
            </div>
          )}
          {FORMATS.map(({ format, label, hint }) => (
            <button key={format} className="export-format" disabled={cadDisabled} onClick={() => start(format)}>
              <span className="export-label">{label}</span>
              <span className="export-hint">{hint}</span>
            </button>
          ))}
          <div className="export-section">
            <button className="export-format" disabled={cadDisabled} onClick={launch}>
              <span className="export-label">Open in Bambu Studio</span>
              <Icon name="links" />
            </button>
            <details className="export-advanced">
              <summary>Bambu options</summary>
              <div className="export-advanced-body">
                <label className="export-checkbox"><input type="checkbox" checked={verifyImport} disabled={pending} onChange={(event) => setVerifyImport(event.target.checked)} /> Verify isolated native import</label>
                <p className="export-hint">Geometry only; keeps slicer presets. Verification uses a separate Bambu session, not your open window. Check scale and placement before slicing.</p>
              </div>
            </details>
          </div>
          <div className="export-section">
            <button className="export-format" disabled={cadDisabled} onClick={preflight}>
              <span className="export-label">Manufacturing preflight</span>
            </button>
            <details className="export-cncjs export-advanced">
              <summary>CNCjs · CNC router G-code</summary>
              <form className="export-advanced-body" onSubmit={(event) => { event.preventDefault(); void cncjs(); }}>
                <p className="export-hint">One flat stock part. Outer and through-hole profiles only; pockets and unsupported holes are skipped and reported.</p>
                <div className="export-cutting-fields">
                  {CUTTING_FIELDS.map(({ key, label }) => (
                    <label key={key}>{label}
                      <input type="number" name={key} aria-label={label} required min="0" step={key === "spindle_speed" ? "1" : "any"}
                        value={cuttingValues[key] ?? ""} disabled={pending}
                        onChange={(event) => { setCuttingValues((values) => ({ ...values, [key]: event.target.value })); setState({ status: "idle" }); }} />
                    </label>
                  ))}
                </div>
                <button type="submit" disabled={cadDisabled || !cuttingValid}>Download CNC router G-code (.nc)</button>
              </form>
            </details>
            {simulation && (
              <button className="export-format" disabled={pending} onClick={gazebo}>
                <span className="export-label">Gazebo + ROS 2</span>
                <span className="export-hint">Full simulation</span>
              </button>
            )}
          </div>
          <div className="export-feedback">
            <div role="status" aria-live="polite">
              {empty ? <span className="export-error">Show an object to export.</span> : !ready ? <span>Loading geometry…</span> : null}
              {state.status === "pending" && <span>{state.action}</span>}
              {state.status === "error" && <span className="export-error">{state.error}</span>}
              {state.status === "complete" && <span>
                {state.launched === undefined ? state.action : `${state.launched ? "Bambu Studio launched." : "Bambu Studio not launched."} ${state.imported?.import_confirmed ? "Isolated import verified." : "Import not confirmed."}`}
              </span>}
            </div>
            <button className="export-details-toggle" aria-label="Export details" aria-expanded={showDetails}
              disabled={!hasDetails && !showDetails} onClick={() => setShowDetails((value) => !value)}>
              Details
            </button>
          </div>
          {showDetails && (
            <div className="export-details">
              {state.status === "complete" && !report && <p>{state.action}</p>}
              {state.status === "error" && <p className="export-error">{state.error}</p>}
              {state.status === "error" && state.log && <pre>{state.log}</pre>}
              {state.status === "complete" && state.imported && <div>
                {state.imported.placement_match !== undefined && <div>Placement: {state.imported.placement_match ? "preserved" : "changed by native import"}.</div>}
                {state.imported.reason && <p>{state.imported.reason}</p>}
                {state.imported.action && <p>{state.imported.action}</p>}
                {state.imported.log && <details><summary>Native import log</summary><pre>{state.imported.log}</pre></details>}
              </div>}
              {report && <>
                <div>{report.object_count} objects · {report.solid_count} solids · {report.bounds_mm.dimensions.map((n) => Number(n.toFixed(3))).join(" × ")} mm</div>
                {report.warnings.length > 0 && <ul>{report.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}
                <p>Geometric estimates only, not slicer results.</p>
                {report.objects.map((object) => <div className="export-object" key={object.path}>
                  <strong>{object.path}</strong>
                  <div>Mesh: {object.mesh.closed_oriented_manifold ? "closed/manifold" : "needs attention"}</div>
                  <div>Bed: {object.bed_placement.within_registered_bed === null ? "not registered" : object.bed_placement.within_registered_bed ? "fits" : "outside envelope"} · {object.bed_placement.on_z0 ? "Z=0 contact" : object.bed_placement.below_z0 ? "below bed" : "above bed"}</div>
                  <div>Overhang: {object.mesh.overhang.area_mm2.toFixed(2)} mm² · bridge extent: {object.mesh.bridge_candidates.max_xy_patch_extent_mm.toFixed(2)} mm</div>
                </div>)}
                <details><summary>Build and evidence limits</summary>
                  <div>Build: {report.build_id ?? "unavailable"}</div>
                  <div>Source: {report.source_revision ?? "unavailable"}</div>
                  {report.limitations.map((limit) => <p key={limit}>{limit}</p>)}
                </details>
              </>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
