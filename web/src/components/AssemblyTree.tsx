import { useEffect, useMemo, useRef, useState } from "react";

import { shapeLeaves } from "../model";
import type { ShapeNode } from "../types";
import { Icon } from "./Icon";

interface Props {
  root: ShapeNode;
  hidden: ReadonlySet<string>;
  selected: string | null;
  onVisibility: (leaves: string[], visible: boolean) => void;
  onSelect: (id: string) => void;
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
  const { root, hidden, selected, onVisibility, onSelect, onHover, openable, onOpen } = props;
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set([root.id]));
  const [filter, setFilter] = useState("");
  const list = useRef<HTMLDivElement>(null);
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

  return (
    <div className="assembly-instances">
      <input
        className="filter"
        placeholder={`Filter ${leaves.length} instances`}
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
      />
      <div className="tree assembly-tree" ref={list} onMouseLeave={() => onHover(null)}>
        {rows.map(({ node, depth }) => {
          const group = node.parts !== undefined;
          const under = group ? shapeLeaves(node).map((leaf) => leaf.id) : [node.id];
          const shownCount = under.filter((id) => !hidden.has(id)).length;
          const visible = shownCount > 0;
          const open = matching !== null || expanded.has(node.id);
          return (
            <div
              key={node.id}
              data-id={node.id}
              className={node.id === selected ? "row selected" : "row"}
              style={{ paddingLeft: 8 + depth * 16 }}
              onMouseEnter={() => onHover(node.id)}
              onClick={() => onSelect(node.id)}
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
                className={shownCount === under.length ? "eye" : visible ? "eye partial" : "eye off"}
                title={visible ? "Hide" : "Show"}
                onClick={(event) => {
                  event.stopPropagation();
                  onVisibility(under, !visible);
                }}
              >
                <Icon name={visible ? "eye" : "eye-off"} />
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
