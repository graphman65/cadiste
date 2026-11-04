import { type KeyboardEvent, useEffect, useMemo, useRef } from "react";

import {
  builderIcon,
  builderTitle,
  countOps,
  location,
  opIcon,
  type TreeIndex,
  type VisibleRow,
  visibleRows,
} from "../model";
import type { TraceData, TreeNode } from "../types";
import { Icon } from "./Icon";
import { ModeBadge } from "./ModeBadge";

interface Props {
  trace: TraceData;
  index: TreeIndex;
  collapsed: ReadonlySet<string>;
  selected: string | null;
  related: ReadonlySet<string>;
  onSelect: (id: string) => void;
  onToggle: (id: string, open?: boolean) => void;
}

export function FeatureTree({ trace, index, collapsed, selected, related, onSelect, onToggle }: Props) {
  const rows = useMemo(() => visibleRows(trace.roots, collapsed), [trace, collapsed]);
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (selected === null) return;
    container.current?.querySelector(`[data-id="${selected}"]`)?.scrollIntoView({ block: "nearest" });
  }, [selected, rows]);

  const onKeyDown = (event: KeyboardEvent) => {
    const at = rows.findIndex((row) => row.node.id === selected);
    const node = at >= 0 ? rows[at].node : null;
    const open = node !== null && node.type !== "op" && !collapsed.has(node.id);
    let handled = true;
    if (event.key === "ArrowDown") onSelect(rows[Math.min(at + 1, rows.length - 1)].node.id);
    else if (event.key === "ArrowUp") onSelect(rows[Math.max(at - 1, 0)].node.id);
    else if (event.key === "ArrowRight" && node !== null && node.type !== "op") {
      if (open) onSelect(node.children[0]?.id ?? node.id);
      else onToggle(node.id, true);
    } else if (event.key === "ArrowLeft" && node !== null) {
      if (open) onToggle(node.id, false);
      else {
        const parent = index.parent.get(node.id);
        if (parent !== undefined) onSelect(parent);
      }
    } else handled = false;
    if (handled) event.preventDefault();
  };

  return (
    <div className="tree" ref={container} tabIndex={0} onKeyDown={onKeyDown} role="tree">
      {rows.map((row) => (
        <Row
          key={row.node.id}
          row={row}
          trace={trace}
          open={!collapsed.has(row.node.id)}
          selected={row.node.id === selected}
          related={related.has(row.node.id)}
          onSelect={onSelect}
          onToggle={onToggle}
        />
      ))}
    </div>
  );
}

interface RowProps {
  row: VisibleRow;
  trace: TraceData;
  open: boolean;
  selected: boolean;
  related: boolean;
  onSelect: (id: string) => void;
  onToggle: (id: string, open?: boolean) => void;
}

function Row({ row, trace, open, selected, related, onSelect, onToggle }: RowProps) {
  const { node, depth } = row;
  const parent = node.type !== "op";
  const classes = ["row", `row-${node.type}`, selected && "selected", related && "related"].filter(Boolean);
  return (
    <div
      className={classes.join(" ")}
      data-id={node.id}
      role="treeitem"
      aria-selected={selected}
      aria-expanded={parent ? open : undefined}
      style={{ paddingLeft: 8 + depth * 16 }}
      onClick={() => onSelect(node.id)}
      onDoubleClick={() => parent && onToggle(node.id)}
    >
      <span
        className={parent ? `caret ${open ? "open" : ""}` : "caret none"}
        onClick={(event) => {
          event.stopPropagation();
          if (parent) onToggle(node.id);
        }}
      >
        {parent ? "▸" : ""}
      </span>
      <RowBody node={node} trace={trace} open={open} />
    </div>
  );
}

function RowBody({ node, trace, open }: { node: TreeNode; trace: TraceData; open: boolean }) {
  switch (node.type) {
    case "op": {
      const op = trace.ops[node.op];
      const site = op.site.at(-1);
      return (
        <>
          <Icon name={opIcon(trace, op)} />
          <span className="index">#{op.index}</span>
          <span className="title" title={site ? location(site) : undefined}>
            {op.kind}
          </span>
          <ModeBadge mode={op.mode} />
        </>
      );
    }
    case "builder": {
      const record = trace.builders[node.builder];
      const site = record.site.at(-1);
      return (
        <>
          <Icon name={builderIcon(record.kind)} className="builder-icon" />
          <span className="title" title={site ? location(site) : undefined}>
            {builderTitle(trace, record.index)}
          </span>
          <ModeBadge mode={record.mode} />
          {!open && <span className="count">{countOps(node)} ops</span>}
        </>
      );
    }
    case "group":
      return (
        <>
          <Icon name="folder" className="group-icon" />
          <span className="title" title={`called at ${location(node.called_at)}`}>
            {node.label}()
          </span>
          {!open && <span className="count">{countOps(node)} ops</span>}
        </>
      );
  }
}
