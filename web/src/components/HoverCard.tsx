import { builderIcon, builderTitle, location, opIcon, type TreeIndex } from "../model";
import type { ShapeNode, TraceData } from "../types";
import { Icon } from "./Icon";
import { ModeBadge } from "./ModeBadge";

/** What the pointer is over in the 3D view, as three-cad-viewer reports it. */
export interface SceneHover {
  /** Viewer path of the component (`.../faces/faces_3`), or null for a message about no component. */
  path: string | null;
  /** Its topology ("face", "edge", "vertex", "solid"), when it is one. */
  topo: string | null;
  /** three-cad-viewer's one-line readout: "plane: area ≈ 3194.40", "circle: r ≈ 2.00, c ≈ (0.00, 1.00, 2.00), ...". */
  readout: string;
}

interface Fact {
  /** Empty for a bare value (a vertex's position). */
  name: string;
  value: string;
  approx: boolean;
}

/** Readout keys: label and unit (build123d models are in millimetres). */
const FACTS: Record<string, [string, string]> = {
  area: ["Area", "mm²"],
  vol: ["Volume", "mm³"],
  len: ["Length", "mm"],
  d: ["Diameter", "mm"],
  r: ["Radius", "mm"],
  c: ["Center", ""],
  at: ["At", ""],
  start: ["Start", ""],
  end: ["End", ""],
  faces: ["Faces", ""],
  edges: ["Edges", ""],
};

/** The parts of `text` between commas outside parentheses. */
function segments(text: string): string[] {
  const found: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    if (text[i] === "(") depth++;
    else if (text[i] === ")") depth--;
    else if (text[i] === "," && depth === 0) {
      found.push(text.slice(start, i).trim());
      start = i + 1;
    }
  }
  found.push(text.slice(start).trim());
  return found.filter((segment) => segment !== "");
}

/**
 * three-cad-viewer's readout as a kind and named facts: "kind: key ≈ value, key=value, N things". A readout it
 * cannot name every part of (a vertex's "(x, y, z)", a copied point) is one bare value.
 */
function parseReadout(readout: string): { kind: string; facts: Fact[] } {
  const colon = readout.indexOf(": ");
  if (colon < 0) return { kind: readout, facts: [] };
  const rest = readout.slice(colon + 2);
  const facts: Fact[] = [];
  for (const segment of segments(rest)) {
    const pair = /^(\w+)\s*(≈|=)\s*(.+)$/.exec(segment);
    const count = /^(\d+) (\w+)$/.exec(segment);
    if (pair) {
      const approx = pair[2] === "≈";
      if (pair[1] === "r") {
        const radius = Number(pair[3]);
        if (Number.isFinite(radius)) {
          facts.push({ name: "d", value: (2 * radius).toFixed(2), approx });
        }
      }
      facts.push({ name: pair[1], value: pair[3], approx });
    } else if (count) facts.push({ name: count[2], value: count[1], approx: false });
    else return { kind: readout.slice(0, colon), facts: [{ name: "", value: rest, approx: false }] };
  }
  return { kind: readout.slice(0, colon), facts };
}

function Value({ value }: { value: string }) {
  const vector = /^\((-?[\d.]+), (-?[\d.]+), (-?[\d.]+)\)$/.exec(value);
  if (vector === null) return <>{value}</>;
  return (
    <span className="xyz">
      <span className="x">{vector[1]}</span>
      <span className="y">{vector[2]}</span>
      <span className="z">{vector[3]}</span>
    </span>
  );
}

/** The component's geometry: its kind ("Plane face"), its number in the part ("f12") and what was measured. */
export function Readout({ hover }: { hover: SceneHover }) {
  const { kind, facts } = parseReadout(hover.readout);
  const title = hover.topo === null || hover.topo === kind ? kind : `${kind} ${hover.topo}`;
  const number = hover.path === null ? null : /\/(face|edge|vertice)s_(\d+)$/.exec(hover.path);
  return (
    <div className="readout">
      <div className="readout-head">
        {title}
        {number && <span className="index">{`${number[1][0]}${number[2]}`}</span>}
      </div>
      {facts.length > 0 && (
        <dl>
          {facts.map((fact, i) => {
            const [label, unit] = FACTS[fact.name] ?? [fact.name, ""];
            return (
              <div key={i}>
                <dt>{label}</dt>
                <dd>
                  {fact.approx && <span className="approx">≈ </span>}
                  <Value value={fact.value} />
                  {unit && <span className="unit"> {unit}</span>}
                </dd>
              </div>
            );
          })}
        </dl>
      )}
    </div>
  );
}

/** The feature tree node a part's component belongs to: the operation that made a face, a sketch, an element. */
export function FeatureHover({ trace, index, node }: { trace: TraceData; index: TreeIndex; node: string }) {
  const found = index.nodes.get(node);
  if (found?.type === "op") {
    const op = trace.ops[found.op];
    return (
      <div className="hover-subject">
        <Icon name={opIcon(trace, op)} />
        <span className="title">{op.kind}</span>
        <span className="index">#{op.index}</span>
        <ModeBadge mode={op.mode} />
        <span className="meta">{location(op.site[op.site.length - 1])}</span>
      </div>
    );
  }
  if (found?.type === "builder") {
    const record = trace.builders[found.builder];
    return (
      <div className="hover-subject">
        <Icon name={builderIcon(record.kind)} />
        <span className="title">{builderTitle(trace, record.index)}</span>
        <ModeBadge mode={record.mode} />
        <span className="meta">{location(record.site[record.site.length - 1])}</span>
      </div>
    );
  }
  return null;
}

/** An assembly instance, under the groups it was built in. */
export function InstanceHover({ leaf }: { leaf: ShapeNode }) {
  const groups = leaf.id.split("/").slice(1, -1);
  return (
    <div className="hover-subject instance">
      <Icon name="part" />
      <span className="title">{leaf.name}</span>
      {groups.length > 0 && <span className="hover-context">{groups.join(" › ")}</span>}
    </div>
  );
}
