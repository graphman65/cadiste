import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import type { SelectionItem } from "../types";
import { CheckReview, type CheckVisualization, type ExactCheckReport } from "./CheckReview";


type State =
  | { key: string; status: "idle" | "loading" }
  | { key: string; status: "error"; error: string }
  | { key: string; status: "result"; result: ExactCheckReport };

/** Explicit selected leaves only: a picked entity identifies its containing leaf, never its neighbours. */
function selectedLeaves(selection: SelectionItem[]): string[] {
  const paths = new Set<string>();
  for (const item of selection) {
    if (item.kind === "instance") paths.add(item.path);
    if (item.kind === "entity") {
      const match = /^(.*)\/(faces|edges|vertices)\/\2_\d+$/.exec(item.path);
      if (match) paths.add(match[1]);
    }
  }
  return [...paths];
}

/** Check exactly two selected solid instances on the retained shapes of the current scene. */
export function SolidCheck({ shapes, selection, onReview }: {
  shapes: string | null;
  selection: SelectionItem[];
  onReview?: (review: CheckVisualization | null) => void;
}) {
  const paths = selectedLeaves(selection);
  const [clearance, setClearance] = useState("0");
  const value = Number(clearance);
  const validClearance = clearance.trim() !== "" && Number.isFinite(value) && value >= 0;
  const key = JSON.stringify([shapes, paths, clearance]);
  const [state, setState] = useState<State>({ key, status: "idle" });
  const pending = useRef<AbortController | null>(null);
  const current = state.key === key ? state : null;
  const loading = current?.status === "loading";
  const enabled = shapes !== null && paths.length === 2 && validClearance && !loading;

  useEffect(() => {
    pending.current?.abort();
    pending.current = null;
    setState({ key, status: "idle" });
    return () => {
      pending.current?.abort();
      pending.current = null;
    };
  }, [key]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!enabled || shapes === null) return;
    pending.current?.abort();
    const abort = new AbortController();
    pending.current = abort;
    setState({ key, status: "loading" });
    try {
      const response = await fetch("/api/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ shapes, pairs: [paths], clearance: value }),
        signal: abort.signal,
      });
      const body = await response.json() as ExactCheckReport & { error?: string };
      if (!response.ok) throw new Error(body.error ?? `Solid check failed (${response.status})`);
      if (typeof body.ok !== "boolean" || !Array.isArray(body.pairs) || body.pairs.length !== 1) {
        throw new Error("The server returned no explicit-pair check result.");
      }
      if (!abort.signal.aborted) setState({ key, status: "result", result: body });
    } catch (error: unknown) {
      if (!abort.signal.aborted) {
        setState({ key, status: "error", error: error instanceof Error ? error.message : String(error) });
      }
    } finally {
      if (pending.current === abort) pending.current = null;
    }
  }

  const result = current?.status === "result" ? current.result : null;
  const pair = result?.pairs[0];
  return (
    <section className="measure-panel readout" aria-label="Solid interference and clearance">
      <div className="readout-head">Solid check</div>
      {shapes === null && <p className="muted">This build has no retained shapes: rebuild it.</p>}
      {paths.length !== 2 && <p className="muted">Select exactly two different instances (or their faces, edges or vertices).</p>}
      {paths.length > 0 && (
        <ul className="measure-picks" aria-label="Explicit solid check instances">
          {paths.map((path) => <li key={path}><span className="name">{path}</span></li>)}
        </ul>
      )}
      <form onSubmit={submit}>
        <label>
          Minimum clearance (mm)
          <input
            type="number" min="0" step="any" value={clearance}
            onChange={(event) => setClearance(event.target.value)}
            aria-invalid={!validClearance}
            required
          />
        </label>
        {!validClearance && <p className="measure-error">Enter a finite, nonnegative clearance.</p>}
        <button type="submit" disabled={!enabled}>{loading ? "Checking…" : "Check"}</button>
      </form>
      <div aria-live="polite" aria-atomic="true" aria-busy={loading}>
        {current?.status === "error" && <p className="measure-error" role="alert">{current.error}</p>}
        {pair && result && (
          <>
            <p>{result.ok ? "Pass" : "Fail"}: {pair.status.replaceAll("_", " ")}</p>
            {pair.available && (
              <dl>
                <dt>Overlap volume</dt><dd>{pair.overlap_volume} mm³</dd>
                <dt>Minimum separation</dt><dd>{pair.minimum_distance} mm</dd>
              </dl>
            )}
            {!pair.available && <p className="measure-error">{pair.error ?? "Exact checks are unavailable for an invalid shape."}</p>}
          </>
        )}
      </div>
      <p className="muted">Only this explicit pair at its recorded pose is checked. Zero separation is not penetration depth.</p>
      {result !== null && onReview !== undefined && <CheckReview result={result} onReview={onReview} />}
    </section>
  );
}
