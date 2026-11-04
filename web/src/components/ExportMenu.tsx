import { useEffect, useRef, useState } from "react";

import type { Target } from "../App";
import { type ExportFormat, fetchExport, TraceFailure } from "../api";
import { Icon } from "./Icon";

const FORMATS: { format: ExportFormat; label: string; hint: string }[] = [
  { format: "step", label: "STEP", hint: "Solids for any CAD tool; an assembly or folder keeps its parts, names and placements" },
  { format: "stl", label: "STL", hint: "One triangle mesh, for slicers and mesh tools" },
  { format: "brep", label: "BREP", hint: "OpenCascade's own B-rep, exact, for build123d, CadQuery or FreeCAD" },
];

type State =
  | { status: "idle" }
  | { status: "exporting"; format: ExportFormat }
  | { status: "error"; error: string; log?: string };

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

/** A folder's parts left shown when some are hidden: only those are exported. */
export interface Shown {
  parts: string[];
  of: number;
}

/**
 * The top bar's Export button: what is open (a part, a folder's parts, an assembly) built again and downloaded; in a
 * folder's view, only the parts shown.
 */
export function ExportMenu({ target, title, shown }: { target: Target | null; title: string | undefined; shown: Shown | null }) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<State>({ status: "idle" });
  const root = useRef<HTMLDivElement>(null);

  // A failure is about the target it was for.
  useEffect(() => setState((current) => (current.status === "error" ? { status: "idle" } : current)), [target]);

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

  const start = (format: ExportFormat) => {
    if (target === null) return;
    setOpen(false);
    setState({ status: "exporting", format });
    fetchExport(target, format, shown?.parts)
      .then(({ file, name }) => {
        download(file, name);
        setState({ status: "idle" });
      })
      .catch((error: unknown) => {
        const log = error instanceof TraceFailure ? error.log : undefined;
        setState({ status: "error", error: error instanceof Error ? error.message : String(error), log });
        setOpen(true);
      });
  };

  const exporting = state.status === "exporting";
  return (
    <div className="export" ref={root}>
      <button
        disabled={target === null || exporting}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((shown) => !shown)}
        title={title ? `Download ${title} as a CAD file` : "Download what is open as a CAD file"}
      >
        {exporting ? <span className="spinner" /> : <Icon name="download" />}
        {exporting ? `Exporting ${state.format.toUpperCase()}…` : "Export"}
      </button>
      {open && (
        <div className="export-menu" role="menu">
          <div className="export-title">
            Export <strong>{title}</strong>
            {shown && ` · ${shown.parts.length} of ${shown.of} parts, the shown ones`}
          </div>
          {FORMATS.map(({ format, label, hint }) => (
            <button
              key={format}
              role="menuitem"
              className="export-format"
              disabled={shown?.parts.length === 0}
              onClick={() => start(format)}
            >
              <span className="export-label">{label}</span>
              <span className="export-hint">{hint}</span>
            </button>
          ))}
          {shown?.parts.length === 0 && <div className="export-error">Every part is hidden: show some to export them.</div>}
          <div className="export-note">Built anew, not traced: the model's own part cache may serve.</div>
          {state.status === "error" && (
            <div className="export-error">
              <div>{state.error}</div>
              {state.log && <pre>{state.log}</pre>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
