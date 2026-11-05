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
import { selectionMode, type SelectionMode, type SelectionModifiers } from "../selection";
import type { TraceData, TreeNode } from "../types";
import { Icon } from "./Icon";
import { ModeBadge } from "./ModeBadge";

interface Props {
  trace: TraceData;
  index: TreeIndex;
  collapsed: ReadonlySet<string>;
  selected: string | null;
  selectedNodes: ReadonlySet<string>;
  part: string;
  related: ReadonlySet<string>;
  onSelect: (id: string, mode?: SelectionMode, range?: string[]) => void;
  onToggle: (id: string, open?: boolean) => void;
}

export function FeatureTree({ trace, index, collapsed, selected, selectedNodes, part, related, onSelect, onToggle }: Props) {
  const rows = useMemo(() => visibleRows(trace.roots, collapsed), [trace, collapsed]);
  const container = useRef<HTMLDivElement>(null);
  const anchor = useRef<string | null>(null);
  const choose = (id: string, event: SelectionModifiers) => {
    container.current?.focus();
    const mode = selectionMode(event);
    const from = rows.findIndex((row) => row.node.id === anchor.current);
    const to = rows.findIndex((row) => row.node.id === id);
    const range = event.shiftKey && from >= 0 && to >= 0
      ? rows.slice(Math.min(from, to), Math.max(from, to) + 1).map((row) => row.node.id)
      : undefined;
    onSelect(id, mode, range);
    if (!event.shiftKey) anchor.current = id;
  };

  useEffect(() => {
    if (selected === null) return;
    container.current?.querySelector(`[data-id="${selected}"]`)?.scrollIntoView({ block: "nearest" });
  }, [selected, rows]);

  const onKeyDown = (event: KeyboardEvent) => {
    if (rows.length === 0 || event.target !== event.currentTarget) return;
    const at = rows.findIndex((row) => row.node.id === selected);
    const node = at >= 0 ? rows[at].node : null;
    const open = node !== null && node.type !== "op" && !collapsed.has(node.id);
    let handled = true;
    if (event.key === "ArrowDown") choose(rows[Math.min(at + 1, rows.length - 1)].node.id, event);
    else if (event.key === "ArrowUp") choose(rows[Math.max(at - 1, 0)].node.id, event);
    else if (event.key === "Home") choose(rows[0].node.id, event);
    else if (event.key === "End") choose(rows.at(-1)!.node.id, event);
    else if (event.key === "ArrowRight" && node !== null && node.type !== "op") {
      if (open) choose(node.children[0]?.id ?? node.id, event);
      else onToggle(node.id, true);
    } else if (event.key === "ArrowLeft" && node !== null) {
      if (open) onToggle(node.id, false);
      else {
        const parent = index.parent.get(node.id);
        if (parent !== undefined) choose(parent, event);
      }
    } else if ((event.key === " " || event.key === "Enter") && node !== null) choose(node.id, event);
    else handled = false;
    if (handled) event.preventDefault();
  };

  return (
    <div className="tree" ref={container} data-part={part} tabIndex={0} onKeyDown={onKeyDown} role="tree" aria-label={`${part} features`} aria-multiselectable="true">
      {rows.map((row) => (
        <Row
          key={row.node.id}
          row={row}
          trace={trace}
          open={!collapsed.has(row.node.id)}
          selected={selectedNodes.has(row.node.id)}
          related={related.has(row.node.id)}
          onSelect={choose}
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
  onSelect: (id: string, event: SelectionModifiers) => void;
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
      onClick={(event) => onSelect(node.id, event)}
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
