import { useEffect, useRef } from "react";

import { ancestors, builderIcon, builderTitle, countOps, location, opIcon, type TreeIndex } from "../model";
import type { TraceData, TreeNode } from "../types";
import { Icon, type IconName } from "./Icon";

interface Props {
  trace: TraceData;
  index: TreeIndex;
  selected: string | null;
  onSelect: (id: string) => void;
}

/** The outermost builder's features left to right, like a CAD timeline; the chip holding the selection is lit. */
export function Timeline({ trace, index, selected, onSelect }: Props) {
  const track = useRef<HTMLDivElement>(null);
  const root = trace.roots[0];
  const lit = new Set(selected === null ? [] : [selected, ...ancestors(index, selected)]);

  useEffect(() => {
    track.current?.querySelector(".chip.lit")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [selected]);

  if (root === undefined || root.type !== "builder") return <footer className="timeline" />;
  return (
    <footer className="timeline">
      <button className="timeline-root" onClick={() => onSelect(root.id)} title="The outermost builder">
        <Icon name="part" />
        {builderTitle(trace, root.builder)}
      </button>
      <div className="timeline-track" ref={track}>
        {root.children.map((node, step) => (
          <Chip key={node.id} node={node} step={step + 1} trace={trace} lit={lit.has(node.id)} onSelect={onSelect} />
        ))}
      </div>
    </footer>
  );
}

function chipFace(node: TreeNode, trace: TraceData): { icon: IconName; label: string; mode: string; tip: string } {
  switch (node.type) {
    case "op": {
      const op = trace.ops[node.op];
      const site = op.site.at(-1);
      return { icon: opIcon(trace, op), label: op.kind, mode: op.mode, tip: site ? location(site) : "" };
    }
    case "builder": {
      const record = trace.builders[node.builder];
      const site = record.site.at(-1);
      return {
        icon: builderIcon(record.kind),
        label: record.name || record.kind,
        mode: record.mode,
        tip: site ? location(site) : "",
      };
    }
    case "group":
      return {
        icon: "folder",
        label: `${node.label}()`,
        mode: "group",
        tip: `${countOps(node)} ops, called at ${location(node.called_at)}`,
      };
  }
}

function Chip(props: { node: TreeNode; step: number; trace: TraceData; lit: boolean; onSelect: (id: string) => void }) {
  const { node, step, trace, lit, onSelect } = props;
  const face = chipFace(node, trace);
  return (
    <button
      className={`chip chip-${face.mode}${lit ? " lit" : ""}`}
      onClick={() => onSelect(node.id)}
      title={`${step}. ${face.label} — ${face.tip}`}
    >
      <Icon name={face.icon} />
      <span>{face.label}</span>
    </button>
  );
}
