import { useEffect, useRef, useState } from "react";
import type { Vec3 } from "../types";

export interface CheckPose {
  path: string;
  translation: Vec3;
  rotation: Vec3;
  /** Exact checker-resolved world pivot, never a mesh bounding-box approximation. */
  origin: Vec3;
}

export interface CheckPair {
  paths: [string, string];
  status: string;
  available: boolean;
  overlap_volume: number | null;
  minimum_distance: number | null;
  witnesses?: [Vec3, Vec3] | null;
  passed: boolean;
  error?: string;
}

export interface CheckSample {
  index: number;
  name?: string;
  poses: CheckPose[];
  pairs: CheckPair[];
  passed: boolean;
}

export interface ExactCheckReport {
  ok: boolean;
  pairs: CheckPair[];
  samples?: CheckSample[];
}

export interface CheckVisualization {
  label: string;
  pair: CheckPair;
  poses: CheckPose[];
}

/** The same review control works for selected-pair checks and named, sampled verification reports. */
export function CheckReview({ result, onReview }: {
  result: ExactCheckReport;
  onReview: (review: CheckVisualization | null) => void;
}) {
  const [selected, setSelected] = useState("");
  const [error, setError] = useState<string | null>(null);
  const review = useRef(onReview);
  review.current = onReview;
  useEffect(() => {
    setSelected("");
    setError(null);
    review.current(null);
    return () => review.current(null);
  }, [result]);
  const rows = [
    ...result.pairs.map((pair, i) => ({ key: `base-${i}`, label: "Recorded pose", pair, poses: [] as CheckPose[] })),
    ...(result.samples ?? []).flatMap((sample) => sample.pairs.map((pair, i) => ({
      key: `${sample.index}-${i}`, label: sample.name ?? `Sample ${sample.index + 1}`, pair, poses: sample.poses,
    }))),
  ];
  return (
    <section className="check-review" aria-label="Exact check witness and pose review">
      <p className="muted">Discrete poses only — no continuous-motion proof. Witnesses mark minimum separation, not penetration depth.</p>
      <ul className="measure-picks">
        {rows.map((row) => (
          <li key={row.key}>
            <button type="button" aria-pressed={selected === row.key} disabled={!row.pair.available} onClick={() => {
              try {
                onReview(selected === row.key ? null : { label: row.label, pair: row.pair, poses: row.poses });
                setSelected(selected === row.key ? "" : row.key);
                setError(null);
              } catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
            }}>
              {row.pair.passed ? "Pass" : "Fail"} · {row.label} · {row.pair.paths.join(" ↔ ")}
            </button>
          </li>
        ))}
      </ul>
      {selected && <button type="button" onClick={() => { onReview(null); setSelected(""); }}>Return to recorded pose</button>}
      {error !== null && <p role="alert" className="measure-error">{error}</p>}
    </section>
  );
}
