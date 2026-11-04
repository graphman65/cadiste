import { useEffect, useRef, useState } from "react";
import type { Viewer } from "three-cad-viewer";

import { fetchMeasurement } from "../api";
import type { MeasuredEntity, Measurement, Quantity, Vec3 } from "../types";
import { Icon } from "./Icon";

/** A face, edge or vertex picked to measure: its viewer path and its id in the viewer's component registry. */
export interface Picked {
  path: string;
  id: number;
  topo: string;
}

export type MeasureState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ok"; data: Measurement }
  | { status: "error"; error: string };

/** The server's measurement of the picked components, on the build's shapes; asked again as the picks change. */
export function useMeasurement(shapes: string | null, picked: Picked[]): MeasureState {
  const [state, setState] = useState<MeasureState>({ status: "idle" });
  const paths = picked.map((p) => p.path).join("\n");
  useEffect(() => {
    if (shapes === null || paths === "") {
      setState({ status: "idle" });
      return;
    }
    const abort = new AbortController();
    setState({ status: "loading" });
    fetchMeasurement(shapes, paths.split("\n"), abort.signal)
      .then((data) => {
        // An abort while the body is read resolves with no body rather than rejecting.
        if (!abort.signal.aborted) setState({ status: "ok", data });
      })
      .catch((error: unknown) => {
        if (abort.signal.aborted) return;
        setState({ status: "error", error: error instanceof Error ? error.message : String(error) });
      });
    return () => abort.abort();
  }, [shapes, paths]);
  return state;
}

/** The measurement, if it is of exactly these picks (a new pick's answer may still be on its way). */
export function measurementOf(state: MeasureState, picked: Picked[]): Measurement | null {
  if (state.status !== "ok") return null;
  const { entities } = state.data;
  const current = entities.length === picked.length && entities.every((entity, i) => entity.path === picked[i].path);
  return current ? state.data : null;
}

const UNITS: Partial<Record<Quantity["kind"], string>> = { length: "mm", area: "mm²", angle: "°" };
const DIGITS: Record<Quantity["kind"], number> = { length: 3, area: 3, angle: 2, point: 3, direction: 3 };

/** The value to the quantity's digits, without a "-0.000" for a coordinate that is zero but for rounding. */
function fixed(value: number, kind: Quantity["kind"]): string {
  const digits = DIGITS[kind];
  return (Math.abs(value) < 0.5 * 10 ** -digits ? 0 : value).toFixed(digits);
}

function QuantityValue({ quantity }: { quantity: Quantity }) {
  const value = quantity.value;
  if (typeof value !== "number") {
    return (
      <span className="xyz">
        {value.map((v, i) => (
          <span key={i} className={"xyz"[i]}>
            {fixed(v, quantity.kind)}
          </span>
        ))}
      </span>
    );
  }
  const unit = UNITS[quantity.kind];
  return (
    <>
      {fixed(value, quantity.kind)}
      {unit && <span className="unit">{quantity.kind === "angle" ? unit : ` ${unit}`}</span>}
    </>
  );
}

function Quantities({ rows }: { rows: Quantity[] }) {
  return (
    <dl>
      {rows.map((row) => (
        <div key={row.label}>
          <dt>{row.label}</dt>
          <dd>
            <QuantityValue quantity={row} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** "Cylinder face", "Arc edge", "Vertex"; its number in its part ("f3"); and the part (the viewer leaf's name). */
function title(picked: Picked, entity: MeasuredEntity | undefined) {
  const kind = entity === undefined || picked.topo === "vertex" ? null : entity.kind;
  const name = kind === null ? picked.topo : `${kind} ${picked.topo}`;
  const match = /^(.*)\/(face|edge|vertice)s\/\2s_(\d+)$/.exec(picked.path);
  return {
    name: name[0].toUpperCase() + name.slice(1),
    number: match && `${match[2][0]}${match[3]}`,
    part: match && match[1].slice(match[1].lastIndexOf("/") + 1),
  };
}

interface PanelProps {
  picked: Picked[];
  state: MeasureState;
  /** Whether the build has shapes to measure on. */
  available: boolean;
  onDrop: (path: string) => void;
  onClear: () => void;
}

/**
 * The picked components and what was measured on them: one's properties; for two, the distances and the angle
 * between them; for several edges or faces, their total length or area.
 */
export function MeasurePanel({ picked, state, available, onDrop, onClear }: PanelProps) {
  const measurement = measurementOf(state, picked);
  const entities = new Map(measurement?.entities.map((entity) => [entity.path, entity]));
  const single = picked.length === 1 ? entities.get(picked[0].path) : undefined;
  return (
    <section className="measure-panel readout" aria-label="Measurements">
      <div className="readout-head">
        Measure
        <span className="spacer" />
        {picked.length > 0 && (
          <button className="measure-clear" onClick={onClear} title="Drop every pick (Esc)">
            Clear
          </button>
        )}
      </div>
      {!available && <p className="muted">This build has no shapes to measure on: rebuild it.</p>}
      {available && picked.length === 0 && (
        <p className="muted">Click faces, edges or vertices to measure them; click one again to drop it.</p>
      )}
      {picked.length > 0 && (
        <ul className="measure-picks">
          {picked.map((pick) => {
            const entity = entities.get(pick.path);
            const { name, number, part } = title(pick, entity);
            const headline = picked.length > 1 ? entity?.properties[0] : undefined;
            return (
              <li key={pick.path}>
                <span className="name">{name}</span>
                {number && <span className="index">{number}</span>}
                {part && (
                  <span className="part" title={pick.path}>
                    {part}
                  </span>
                )}
                <span className="spacer" />
                {headline && (
                  <span className="headline" title={headline.label}>
                    <QuantityValue quantity={headline} />
                  </span>
                )}
                <button className="icon-button" onClick={() => onDrop(pick.path)} title="Drop it">
                  <Icon name="close" />
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {single && <Quantities rows={single.properties} />}
      {measurement && measurement.results.length > 0 && <Quantities rows={measurement.results} />}
      {state.status === "loading" && <p className="muted">Measuring…</p>}
      {state.status === "error" && <p className="measure-error">{state.error}</p>}
    </section>
  );
}

type Point2 = [number, number];

interface Mark {
  key: string;
  /** The minimum distance, drawn solid; the others dashed. */
  primary: boolean;
  from: Point2;
  to: Point2;
  text: string;
}

/** Points to draw: the segments the distances span, labelled, and the points measured (centres, positions). */
function marksOf(measurement: Measurement | null): { lines: Quantity[]; points: Vec3[] } {
  if (measurement === null) return { lines: [], points: [] };
  const rows = [...measurement.results, ...measurement.entities.flatMap((entity) => entity.properties)];
  return {
    lines: rows.filter((row) => row.line !== undefined),
    points: rows.filter((row) => row.kind === "point").map((row) => row.value as Vec3),
  };
}

/**
 * The measured distances drawn over the 3D view, as dimension lines from one measured point to the other, with their
 * value; follows the camera every frame.
 */
export function MeasureMarks({ viewer, measurement }: { viewer: () => Viewer | null; measurement: Measurement | null }) {
  const svg = useRef<SVGSVGElement>(null);
  const [drawn, setDrawn] = useState<{ lines: Mark[]; points: Point2[] }>({ lines: [], points: [] });

  useEffect(() => {
    const { lines, points } = marksOf(measurement);
    if (lines.length === 0 && points.length === 0) {
      setDrawn({ lines: [], points: [] });
      return;
    }
    let frame = 0;
    let last = "";
    const tick = () => {
      frame = requestAnimationFrame(tick);
      const current = viewer();
      const element = svg.current;
      if (current === null || !current.ready || element === null) return;
      const camera = current.camera.getCamera();
      const canvas = (current.renderer.domElement as HTMLCanvasElement).getBoundingClientRect();
      const box = element.getBoundingClientRect();
      const view: ArrayLike<number> = camera.matrixWorldInverse.elements;
      const projection: ArrayLike<number> = camera.projectionMatrix.elements;
      const apply = (m: ArrayLike<number>, [x, y, z, w]: number[]) => [
        m[0] * x + m[4] * y + m[8] * z + m[12] * w,
        m[1] * x + m[5] * y + m[9] * z + m[13] * w,
        m[2] * x + m[6] * y + m[10] * z + m[14] * w,
        m[3] * x + m[7] * y + m[11] * z + m[15] * w,
      ];
      const project = (p: Vec3): Point2 | null => {
        const [x, y, , w] = apply(projection, apply(view, [...p, 1]));
        if (w <= 0) return null; // behind a perspective camera
        return [
          canvas.left - box.left + ((x / w + 1) / 2) * canvas.width,
          canvas.top - box.top + ((1 - y / w) / 2) * canvas.height,
        ];
      };
      const next = { lines: [] as Mark[], points: [] as Point2[] };
      for (const row of lines) {
        const [a, b] = row.line as [Vec3, Vec3];
        const from = project(a);
        const to = project(b);
        if (from && to) {
          const text = `${fixed(row.value as number, "length")} mm`;
          next.lines.push({ key: row.label, primary: row.label === "Minimum distance", from, to, text });
        }
      }
      for (const p of points) {
        const at = project(p);
        if (at) next.points.push(at);
      }
      const signature = JSON.stringify(next);
      if (signature !== last) {
        last = signature;
        setDrawn(next);
      }
    };
    tick();
    return () => cancelAnimationFrame(frame);
  }, [viewer, measurement]);

  return (
    <svg className="measure-marks" ref={svg} aria-hidden="true">
      {drawn.points.map(([x, y], i) => (
        <circle key={i} className="measure-point" cx={x} cy={y} r={3} />
      ))}
      {drawn.lines.map(({ key, primary, from, to, text }) => {
        const [x, y] = [(from[0] + to[0]) / 2, (from[1] + to[1]) / 2];
        return (
          <g key={key} className={primary ? "measure-line primary" : "measure-line"}>
            <line x1={from[0]} y1={from[1]} x2={to[0]} y2={to[1]} />
            <circle cx={from[0]} cy={from[1]} r={2.5} />
            <circle cx={to[0]} cy={to[1]} r={2.5} />
            {/* The minimum distance's value above its line, the others' below: near-parallel lines stay legible. */}
            <text x={x} y={primary ? y - 8 : y + 16} textAnchor="middle">
              {text}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
