import { type ReactNode, useState } from "react";

import {
  builderIcon,
  builderTitle,
  location,
  opIcon,
  opNodeId,
  type TreeIndex,
} from "../model";
import { usePersistentSet } from "../persist";
import type { BuilderData, OpData, Site, TraceData, TreeNode } from "../types";
import { Icon, type IconName } from "./Icon";
import { ModeBadge } from "./ModeBadge";
import { SourceView } from "./SourceView";

interface Props {
  trace: TraceData;
  index: TreeIndex;
  selected: string | null;
  onSelect: (id: string) => void;
  onHide: () => void;
}

/** The sections the toolbar shows or hides; the arguments (an operation) or contents (a builder, a group) always show. */
type Part = "links" | "faces" | "stack" | "source";

const PARTS: { part: Part; icon: IconName; title: string }[] = [
  { part: "links", icon: "links", title: "Uses / used by" },
  { part: "faces", icon: "faces", title: "Faces of the result" },
  { part: "stack", icon: "stack", title: "Call stack" },
  { part: "source", icon: "code", title: "Source" },
];

/** The sections a node has: the toolbar disables the others. */
function partsOf(trace: TraceData, node: TreeNode): ReadonlySet<Part> {
  switch (node.type) {
    case "op": {
      const op = trace.ops[node.op];
      const parts: Part[] = ["faces"];
      if (op.inputs.length > 0) parts.push("links");
      if (op.site.length > 0) parts.push("stack", "source");
      return new Set(parts);
    }
    case "builder": {
      const record = trace.builders[node.builder];
      const parts: Part[] = [];
      if (record.published_by !== null || record.consumed_by.length > 0) parts.push("links");
      if (record.site.length > 0) parts.push("stack", "source");
      return new Set(parts);
    }
    case "group":
      return new Set(["stack", "source"]);
  }
}

export function Details({ trace, index, selected, onSelect, onHide }: Props) {
  const [toggled, toggle] = usePersistentSet("cadiste.details-sections");
  const node = selected === null ? undefined : index.nodes.get(selected);
  const available = node === undefined ? new Set<Part>() : partsOf(trace, node);
  const shown = toggled as ReadonlySet<Part>;
  return (
    <aside className="details">
      <div className="details-bar" role="toolbar" aria-label="Details sections">
        {PARTS.map(({ part, icon, title }) => (
          <button
            key={part}
            className={shown.has(part) ? "icon-button on" : "icon-button"}
            aria-pressed={shown.has(part)}
            disabled={!available.has(part)}
            onClick={() => toggle(part)}
            title={title}
          >
            <Icon name={icon} />
          </button>
        ))}
        <span className="spacer" />
        <HidePanelButton onHide={onHide} />
      </div>
      {node === undefined && <div className="empty">Select a feature to see its arguments, inputs and source.</div>}
      {/* Keyed by node so the call-stack focus starts over for every selection. */}
      {node?.type === "op" && (
        <OpDetails key={node.id} trace={trace} op={trace.ops[node.op]} shown={shown} onSelect={onSelect} />
      )}
      {node?.type === "builder" && (
        <BuilderDetails
          key={node.id}
          trace={trace}
          record={trace.builders[node.builder]}
          shown={shown}
          onSelect={onSelect}
        />
      )}
      {node?.type === "group" && <GroupDetails key={node.id} trace={trace} node={node} shown={shown} />}
    </aside>
  );
}

/** Folds the right-hand panel down to its show button. */
export function HidePanelButton({ onHide }: { onHide: () => void }) {
  return (
    <button className="icon-button" onClick={onHide} title="Hide the panel">
      <Icon name="panel-hide" />
    </button>
  );
}

/** A titled block; `grow` lets it take the panel's remaining height (the source). */
function Section({ title, grow = false, children }: { title: string; grow?: boolean; children: ReactNode }) {
  return (
    <section className={grow ? "grow" : undefined}>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function Link({ children, onClick }: { children: ReactNode; onClick: () => void }) {
  return (
    <button className="link" onClick={onClick}>
      {children}
    </button>
  );
}

function OpLink({ trace, op, onSelect }: { trace: TraceData; op: number; onSelect: (id: string) => void }) {
  const data = trace.ops[op];
  return (
    <Link onClick={() => onSelect(opNodeId(trace, op))}>
      <Icon name={opIcon(trace, data)} /> #{data.index} {data.kind}
    </Link>
  );
}

function BuilderLink(props: { trace: TraceData; builder: number; onSelect: (id: string) => void }) {
  const { trace, builder, onSelect } = props;
  return (
    <Link onClick={() => onSelect(`b${builder}`)}>
      <Icon name={builderIcon(trace.builders[builder].kind)} /> {builderTitle(trace, builder)}
    </Link>
  );
}

function FaceList({ faces }: { faces: number[] }) {
  const shown = faces.slice(0, 48);
  return (
    <div className="faces">
      {shown.map((face) => (
        <span key={face} className="face">
          f{face}
        </span>
      ))}
      {faces.length > shown.length && <span className="more">+{faces.length - shown.length} more</span>}
    </div>
  );
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function CallStack({ sites, focus, onFocus }: { sites: Site[]; focus: number; onFocus: (i: number) => void }) {
  return (
    <ol className="stack">
      {sites.map((site, i) => (
        <li key={i} className={i === focus ? "active" : undefined}>
          <button onClick={() => onFocus(i)}>
            <span className="stack-label">{site.label}</span>
            <span className="meta">{location(site)}</span>
          </button>
        </li>
      ))}
    </ol>
  );
}

function OpDetails(props: { trace: TraceData; op: OpData; shown: ReadonlySet<Part>; onSelect: (id: string) => void }) {
  const { trace, op, shown, onSelect } = props;
  const [focus, setFocus] = useState(op.site.length - 1);
  const builder = trace.builders[op.builder];
  const site = op.site[focus];
  const inSketch = builder.kind !== "BuildPart";
  return (
    <>
      <header className="details-head">
        <Icon name={opIcon(trace, op)} />
        <h2>{op.kind}</h2>
        <span className="index">#{op.index}</span>
        <ModeBadge mode={op.mode} long />
      </header>
      <p className="context">
        in <BuilderLink trace={trace} builder={op.builder} onSelect={onSelect} />
      </p>

      <Section title="Arguments">
        {op.arguments.length === 0 ? (
          <p className="muted">None recorded.</p>
        ) : (
          <table className="arguments">
            <tbody>
              {op.arguments.map((argument) => (
                <tr key={argument.name} className={argument.source ? undefined : "default"}>
                  <th>{argument.name}</th>
                  <td>
                    {argument.source ? (
                      <code>{argument.source}</code>
                    ) : (
                      <span className="muted" title="Not written in the call: the operation's own value">
                        not passed
                      </span>
                    )}
                  </td>
                  <td className="value">
                    {/* The value is not repeated when the expression already reads as it (a literal). */}
                    {argument.source.replaceAll('"', "'") === argument.value ? null : <code>{argument.value}</code>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      {shown.has("links") && op.inputs.length > 0 && (
        <Section title="Uses">
          <ul className="links">
            {op.inputs.map((input) => (
              <li key={input}>
                <BuilderLink trace={trace} builder={input} onSelect={onSelect} />
              </li>
            ))}
          </ul>
        </Section>
      )}

      {shown.has("faces") && (
        <Section title="Faces of the result">
          {inSketch ? (
            <p className="muted">A sketch element: the operation that consumes the sketch makes the faces.</p>
          ) : op.faces.length === 0 && op.via_faces.length === 0 ? (
            <p className="muted">None: later operations removed every face this one made.</p>
          ) : (
            <>
              {op.faces.length > 0 && (
                <>
                  <p>Made {plural(op.faces.length, "face")}.</p>
                  <FaceList faces={op.faces} />
                </>
              )}
              {op.via_faces.length > 0 && (
                <p>Brought in {plural(op.via_faces.length, "face")} made inside the part it inserted.</p>
              )}
            </>
          )}
        </Section>
      )}

      {shown.has("stack") && (
        <Section title="Call stack">
          <CallStack sites={op.site} focus={focus} onFocus={setFocus} />
        </Section>
      )}
      {shown.has("source") && site && (
        <Section title="Source" grow>
          <SourceView site={site} text={trace.sources[site.file]} />
        </Section>
      )}
    </>
  );
}

function BuilderDetails(props: {
  trace: TraceData;
  record: BuilderData;
  shown: ReadonlySet<Part>;
  onSelect: (id: string) => void;
}) {
  const { trace, record, shown, onSelect } = props;
  const [focus, setFocus] = useState(record.site.length - 1);
  const site = record.site[focus];
  const faces = record.ops.reduce((sum, op) => sum + trace.ops[op].faces.length + trace.ops[op].via_faces.length, 0);
  return (
    <>
      <header className="details-head">
        <Icon name={builderIcon(record.kind)} />
        <h2>{builderTitle(trace, record.index)}</h2>
        <ModeBadge mode={record.mode} long />
      </header>
      <p className="context">
        {record.kind}
        {record.owner && <> built by {record.owner}</>}
        {record.parent !== null && (
          <>
            {" "}
            in <BuilderLink trace={trace} builder={record.parent} onSelect={onSelect} />
          </>
        )}
      </p>

      <Section title="Contents">
        <p>{plural(record.ops.length, "operation")}.</p>
        {record.kind === "BuildPart" && faces > 0 && <p>{plural(faces, "face")} of the result come from them.</p>}
      </Section>

      {shown.has("links") && (record.published_by !== null || record.consumed_by.length > 0) && (
        <Section title="Used by">
          <ul className="links">
            {record.published_by !== null && record.parent !== null && (
              <li>
                handed to <BuilderLink trace={trace} builder={record.parent} onSelect={onSelect} /> at exit (
                {trace.ops[record.published_by].mode})
              </li>
            )}
            {record.consumed_by.map((op) => (
              <li key={op}>
                <OpLink trace={trace} op={op} onSelect={onSelect} />
              </li>
            ))}
          </ul>
        </Section>
      )}

      {shown.has("stack") && record.site.length > 0 && (
        <Section title="Call stack">
          <CallStack sites={record.site} focus={focus} onFocus={setFocus} />
        </Section>
      )}
      {shown.has("source") && site && (
        <Section title="Source" grow>
          <SourceView site={site} text={trace.sources[site.file]} />
        </Section>
      )}
    </>
  );
}

function collectOps(node: TreeNode, into: number[] = []): number[] {
  if (node.type === "op") into.push(node.op);
  else node.children.forEach((child) => collectOps(child, into));
  return into;
}

function GroupDetails(props: { trace: TraceData; node: Extract<TreeNode, { type: "group" }>; shown: ReadonlySet<Part> }) {
  const { trace, node, shown } = props;
  const ops = collectOps(node).map((op) => trace.ops[op]);
  const kinds = new Map<string, number>();
  ops.forEach((op) => kinds.set(op.kind, (kinds.get(op.kind) ?? 0) + 1));
  const faces = ops.reduce((sum, op) => sum + op.faces.length + op.via_faces.length, 0);
  return (
    <>
      <header className="details-head">
        <Icon name="folder" />
        <h2>{node.label}()</h2>
      </header>
      <p className="context">
        <code>{node.function}</code> in {node.defined_in}
      </p>
      <Section title="Contents">
        <p>{plural(ops.length, "operation")}:</p>
        <ul className="kinds">
          {[...kinds].map(([kind, count]) => (
            <li key={kind}>
              {kind}
              {count > 1 && <span className="muted"> ×{count}</span>}
            </li>
          ))}
        </ul>
        {faces > 0 && <p>{plural(faces, "face")} of the result come from them.</p>}
      </Section>
      {shown.has("stack") && (
        <Section title="Called at">
          <p>
            <code>{location(node.called_at)}</code> in {node.called_at.label}
          </p>
        </Section>
      )}
      {shown.has("source") && (
        <Section title="Source" grow>
          <SourceView site={node.called_at} text={trace.sources[node.called_at.file]} />
        </Section>
      )}
    </>
  );
}
