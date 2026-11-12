import { useEffect, useMemo, useRef, useState } from "react";

import { shapeLeaves } from "../model";
import { selectionMode, type SelectionMode, type SelectionModifiers } from "../selection";
import type { ShapeNode } from "../types";
import { Icon } from "./Icon";

interface Props {
  root: ShapeNode;
  hidden: ReadonlySet<string>;
  solo: string | null;
  onSolo: (id: string) => void;
  selected: string | null;
  selectedPaths: ReadonlySet<string>;
  onVisibility: (leaves: string[], visible: boolean) => void;
  onSelect: (id: string, mode?: SelectionMode, range?: string[]) => void;
  onHover: (id: string | null) => void;
  /** Rows named like a part (a file's parts are) can open it: a button, or a double-click. */
  openable: ReadonlySet<string>;
  onOpen: (part: string) => void;
}

/** Viewer ids are paths ("/Pipe Crawler/Front drive module/..."): a node's ancestors are its prefixes. */
function ancestorIds(id: string): string[] {
  const parts = id.split("/").slice(1);
  return parts.slice(0, -1).map((_, i) => `/${parts.slice(0, i + 1).join("/")}`);
}

/** The assembly as built: groups and part instances, with visibility and selection driving the 3D view. */
export function AssemblyTree(props: Props) {
  const { root, hidden, selected, selectedPaths, onVisibility, onSelect, onHover, openable, onOpen } = props;
  const { solo, onSolo } = props;
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set([root.id]));
  const [filter, setFilter] = useState("");
  const list = useRef<HTMLDivElement>(null);
  const anchor = useRef<string | null>(null);
  const leaves = useMemo(() => shapeLeaves(root), [root]);

  useEffect(() => {
    if (selected === null) return;
    setExpanded((current) => {
      const missing = ancestorIds(selected).filter((id) => !current.has(id));
      return missing.length === 0 ? current : new Set([...current, ...missing]);
    });
  }, [selected]);

  useEffect(() => {
    if (selected !== null) list.current?.querySelector(`[data-id="${CSS.escape(selected)}"]`)?.scrollIntoView({ block: "nearest" });
  }, [selected, expanded]);

  const needle = filter.trim().toLowerCase();
  // While filtering: the matching instances, inside their (opened) groups.
  const matching = useMemo(() => {
    if (needle === "") return null;
    const shown = new Set<string>();
    for (const leaf of leaves) {
      if (leaf.name.toLowerCase().includes(needle)) [leaf.id, ...ancestorIds(leaf.id)].forEach((id) => shown.add(id));
    }
    return shown;
  }, [leaves, needle]);

  const toggle = (id: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const rows: { node: ShapeNode; depth: number }[] = [];
  const visit = (node: ShapeNode, depth: number) => {
    if (matching !== null && !matching.has(node.id)) return;
    rows.push({ node, depth });
    const open = matching !== null || expanded.has(node.id);
    if (node.parts !== undefined && open) node.parts.forEach((child) => visit(child, depth + 1));
  };
  visit(root, 0);
  const choose = (id: string, event: SelectionModifiers) => {
    const from = rows.findIndex(({ node }) => node.id === anchor.current);
    const to = rows.findIndex(({ node }) => node.id === id);
    const range = event.shiftKey && from >= 0 && to >= 0
      ? rows.slice(Math.min(from, to), Math.max(from, to) + 1).map(({ node }) => node.id) : undefined;
    onSelect(id, selectionMode(event), range);
    if (!event.shiftKey) anchor.current = id;
  };

  return (
    <div className="assembly-instances">
      <input
        className="filter"
        placeholder={`Filter ${leaves.length} instances`}
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
      />
      <div className="tree assembly-tree" ref={list} role="tree" aria-label="Assembly instances" aria-multiselectable="true" tabIndex={0}
        onMouseLeave={() => onHover(null)}
        onKeyDown={(event) => {
          if (event.target !== event.currentTarget) return;
          const at = rows.findIndex(({ node }) => node.id === selected);
          const node = rows[at]?.node;
          let next: string | undefined;
          if (event.key === "ArrowDown") next = rows[Math.min(at + 1, rows.length - 1)]?.node.id;
          else if (event.key === "ArrowUp") next = rows[Math.max(at - 1, 0)]?.node.id;
          else if (event.key === "Home") next = rows[0]?.node.id;
          else if (event.key === "End") next = rows.at(-1)?.node.id;
          else if (event.key === "ArrowRight" && node?.parts) {
            if (!expanded.has(node.id)) toggle(node.id);
            else next = node.parts[0]?.id;
          } else if (event.key === "ArrowLeft" && node) {
            if (node.parts && expanded.has(node.id)) toggle(node.id);
            else next = ancestorIds(node.id).at(-1);
          } else if ((event.key === " " || event.key === "Enter") && node) next = node.id;
          else return;
          event.preventDefault();
          if (next !== undefined) choose(next, event);
        }}>
        {rows.map(({ node, depth }) => {
          const group = node.parts !== undefined;
          const under = group ? shapeLeaves(node).map((leaf) => leaf.id) : [node.id];
          const shownCount = under.filter((id) => !hidden.has(id)).length;
          const visible = shownCount > 0;
          const open = matching !== null || expanded.has(node.id);
          const soloed = solo === node.id;
          return (
            <div
              key={node.id}
              data-id={node.id}
              className={selectedPaths.has(node.id) ? "row selected" : "row"}
              role="treeitem"
              aria-selected={selectedPaths.has(node.id)}
              aria-expanded={group ? open : undefined}
              style={{ paddingLeft: 8 + depth * 16 }}
              onMouseEnter={() => onHover(node.id)}
              onClick={(event) => { list.current?.focus(); choose(node.id, event); }}
              onDoubleClick={() => (group ? toggle(node.id) : openable.has(node.name) && onOpen(node.name))}
            >
              <span
                className={group ? `caret ${open ? "open" : ""}` : "caret none"}
                onClick={(event) => {
                  event.stopPropagation();
                  if (group) toggle(node.id);
                }}
              >
                {group ? "▸" : ""}
              </span>
              <button
                className={`${shownCount === under.length ? "eye" : visible ? "eye partial" : "eye off"}${solo !== null ? " overridden" : ""}`}
                title={`${visible ? "Hide" : "Show"}${solo !== null ? " (temporarily overridden by solo)" : ""}`}
                onClick={(event) => {
                  event.stopPropagation();
                  onVisibility(under, !visible);
                }}
              >
                <Icon name={visible ? "eye" : "eye-off"} />
              </button>
              <button
                className={soloed ? "eye solo active" : "eye solo"}
                aria-label={`Solo ${node.name}`}
                aria-pressed={soloed}
                title={soloed ? "Disable solo; restore eye settings" : `Solo ${node.name}: show only this entry`}
                onClick={(event) => {
                  event.stopPropagation();
                  onSolo(node.id);
                }}
              >
                <Icon name="solo" />
              </button>
              <span className={visible ? "title" : "title muted"}>{node.name}</span>
              {group && <span className="count">{under.length}</span>}
              {!group && openable.has(node.name) && (
                <button
                  className="open-part"
                  title={`Open ${node.name}: its features, sketches and source`}
                  onClick={(event) => {
                    event.stopPropagation();
                    onOpen(node.name);
                  }}
                >
                  Open
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
