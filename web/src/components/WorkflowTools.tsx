import { useEffect, useRef, useState } from "react";

import type { Target } from "../App";
import { type BaselineEntry, fetchBaselines, pinBaseline, postJson, releaseBaseline } from "../api";
import type { SelectionItem } from "../types";

interface Props {
  target: Target | null;
  buildId: string | null;
  checkSets: string[];
  selection: SelectionItem[];
  disabled: boolean;
  onCompare: (baseline: BaselineEntry) => Promise<void>;
  onResult: (report: Record<string, unknown>) => void;
}

/** Exact displayed-build pinning and explicit project check sets; never infer interfaces from names. */
export function WorkflowTools({ target, buildId, checkSets, selection, disabled, onCompare, onResult }: Props) {
  const [baselines, setBaselines] = useState<BaselineEntry[]>([]);
  const [chosen, setChosen] = useState("");
  const [name, setName] = useState("");
  const [checkSet, setCheckSet] = useState("");
  const [preserve, setPreserve] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  useEffect(() => { generation.current += 1; setChosen(""); setError(null); setPreserve(false); }, [target]);
  const applicable = baselines.filter((entry) => target !== null && entry.targets.some((item) => JSON.stringify(item.target) === JSON.stringify(target)));
  const selected = applicable.find((entry) => entry.id === chosen);
  const selectedSet = checkSets.includes(checkSet) ? checkSet : checkSets[0] ?? "";
  const paths = selection.flatMap((item) => item.kind === "feature" ? [] : [item.path]);
  const busy = disabled || pending;
  const refresh = async () => setBaselines(await fetchBaselines());
  const run = async (action: () => Promise<void>) => {
    const started = generation.current;
    setPending(true);
    setError(null);
    try { await action(); }
    catch (error) { if (generation.current === started) setError(error instanceof Error ? error.message : String(error)); }
    finally { setPending(false); }
  };
  return <details className="workflow-tools" onToggle={(event) => {
    if (event.currentTarget.open) void run(refresh);
  }}>
    <summary>Verify</summary>
    <div className="workflow-tools-panel">
      <fieldset disabled={busy || target === null || buildId === null}>
        <legend>Comparison baseline</legend>
        <label>Name <input aria-label="Baseline name" value={name} onChange={(event) => setName(event.target.value)} placeholder="Before this edit" /></label>
        <button onClick={() => void run(async () => {
          if (target === null || buildId === null) return;
          const pinned = await pinBaseline(target, buildId, name.trim() || undefined);
          await refresh(); setChosen(pinned.id);
        })}>Pin baseline</button>
        <label>Baseline <select aria-label="Baseline" value={selected?.id ?? ""} onChange={(event) => setChosen(event.target.value)}>
          <option value="">Choose a pinned build</option>
          {applicable.map((entry) => <option key={entry.id} value={entry.id}>{entry.name || entry.id}</option>)}
        </select></label>
        <button disabled={selected === undefined} onClick={() => void run(async () => { if (selected) await onCompare(selected); })}>Compare baseline</button>
        <button disabled={selected === undefined} onClick={() => void run(async () => {
          if (!selected) return;
          await releaseBaseline(selected.id); setChosen(""); await refresh();
        })}>Release baseline</button>
      </fieldset>
      <fieldset disabled={busy || target === null || buildId === null}>
        <legend>Mechanical checks</legend>
        <label>Check set <select aria-label="Mechanical check set" value={selectedSet} onChange={(event) => setCheckSet(event.target.value)}>
          {checkSets.length === 0 && <option value="">No check sets for this target</option>}
          {checkSets.map((name) => <option key={name} value={name}>{name}</option>)}
        </select></label>
        <label><input type="checkbox" checked={preserve} disabled={!selected || paths.length === 0}
          onChange={(event) => setPreserve(event.target.checked)} />Preserve selected interface against baseline</label>
        <button disabled={!selectedSet && !(preserve && selected && paths.length > 0)} onClick={() => void run(async () => {
          const started = generation.current;
          const report = await postJson<Record<string, unknown>>("/api/verify", {
            targets: [target], baseline: selected?.id,
            check_sets: selectedSet ? [selectedSet] : [],
            assertions: preserve && selected && paths.length > 0 ? [{ kind: "preserved", paths }] : [],
          });
          if (started === generation.current) onResult(report);
        })}>Run checks</button>
      </fieldset>
      <div className="workflow-message" role="status">{pending ? "Working…" : error ?? ""}</div>
    </div>
  </details>;
}
