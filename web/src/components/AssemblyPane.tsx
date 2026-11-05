import { useEffect, useMemo, useRef, useState } from "react";

import { shapeLeaves } from "../model";
import { usePersistentChoice } from "../persist";
import { selectionMode, type SelectionMode, type SelectionModifiers } from "../selection";
import type { AssemblyData } from "../types";
import { AssemblyTree } from "./AssemblyTree";
import { Icon } from "./Icon";

interface Props {
  name: string;
  data: AssemblyData;
  /** The project's parts, in its order: the Parts tab lists those the assembly has instances of in this order. */
  parts: string[];
  hidden: ReadonlySet<string>;
  /** The selected instance-tree node; the part whose instances are all lit; the part shown selected: the lit one, or
   * the selected instance's. */
  instance: string | null;
  marked: string | null;
  selectedPaths: ReadonlySet<string>;
  onVisibility: (leaves: string[], visible: boolean) => void;
  onSelectInstance: (id: string, mode?: SelectionMode, range?: string[]) => void;
  onSelectPart: (part: string, mode?: SelectionMode, range?: string[]) => void;
  onHover: (id: string | null) => void;
  onOpen: (part: string) => void;
}

const TABS = ["parts", "instances"] as const;

/** An assembly's sidebar: its parts (every instance of each at once) or its instances as built. */
export function AssemblyPane(props: Props) {
  const { name, data, parts, hidden, instance, marked, selectedPaths, onVisibility, onSelectInstance, onSelectPart } = props;
  const { onHover, onOpen } = props;
  const [tab, setTab] = usePersistentChoice("cadiste.assembly-tab", TABS);
  const root = data.scene.data.shapes;
  const leaves = useMemo(() => shapeLeaves(root), [root]);
  const hiddenHere = leaves.filter((leaf) => hidden.has(leaf.id)).length;
  const openable = useMemo(() => new Set(parts), [parts]);
  return (
    <div className="assembly-pane">
      <div className="assembly-pane-head">
        <Icon name="assembly" className="builder-icon" />
        <span className="assembly-pane-title">{name}</span>
        <span className="part-status">
          {leaves.length} instances{hiddenHere > 0 && ` · ${hiddenHere} hidden`}
        </span>
        {hiddenHere > 0 && (
          <button className="link show-all" onClick={() => onVisibility(leaves.map((leaf) => leaf.id), true)}>
            Show all
          </button>
        )}
      </div>
      <div className="tabs" role="tablist">
        {TABS.map((each) => (
          <button
            key={each}
            role="tab"
            aria-selected={tab === each}
            className={tab === each ? "tab active" : "tab"}
            onClick={() => setTab(each)}
          >
            {each === "parts" ? "Parts" : "Instances"}
          </button>
        ))}
      </div>
      {tab === "parts" ? (
        <AssemblyParts
          data={data}
          parts={parts}
          hidden={hidden}
          selectedPaths={selectedPaths}
          marked={marked}
          onVisibility={onVisibility}
          onSelect={onSelectPart}
          onOpen={onOpen}
        />
      ) : (
        <AssemblyTree
          root={root}
          hidden={hidden}
          selected={instance}
          selectedPaths={selectedPaths}
          onVisibility={onVisibility}
          onSelect={onSelectInstance}
          onHover={onHover}
          openable={openable}
          onOpen={onOpen}
        />
      )}
    </div>
  );
}

interface PartsProps {
  data: AssemblyData;
  parts: string[];
  hidden: ReadonlySet<string>;
  selectedPaths: ReadonlySet<string>;
  onVisibility: (leaves: string[], visible: boolean) => void;
  marked: string | null;
  onSelect: (part: string, mode?: SelectionMode, range?: string[]) => void;
  onOpen: (part: string) => void;
}

/** The assembly's parts, each with its instances: hide or show them all, light them all, open the part. */
function AssemblyParts({ data, parts, hidden, selectedPaths, marked, onVisibility, onSelect, onOpen }: PartsProps) {
  const [filter, setFilter] = useState("");
  const list = useRef<HTMLUListElement>(null);
  const anchor = useRef<string | null>(null);
  useEffect(() => {
    if (marked !== null) {
      list.current?.querySelector(`[data-part="${CSS.escape(marked)}"]`)?.scrollIntoView({ block: "nearest" });
    }
  }, [marked]);
  const partOf = data.scene.part_of;
  // Part -> its instances (leaf ids), in the project's order of parts.
  const instances = useMemo(() => {
    const byPart = new Map<string, string[]>();
    for (const [leaf, part] of Object.entries(partOf ?? {})) byPart.set(part, [...(byPart.get(part) ?? []), leaf]);
    return parts.filter((part) => byPart.has(part)).map((part) => ({ part, leaves: byPart.get(part) ?? [] }));
  }, [partOf, parts]);
  if (partOf === undefined) {
    return (
      <p className="assembly-note">
        The server does not know which part each instance is (serve with --parts). The Instances tab lists them.
      </p>
    );
  }
  const needle = filter.trim().toLowerCase();
  const shown = instances.filter(({ part }) => part.toLowerCase().includes(needle));
  const choose = (part: string, event: SelectionModifiers) => {
    const from = shown.findIndex((row) => row.part === anchor.current);
    const to = shown.findIndex((row) => row.part === part);
    const range = event.shiftKey && from >= 0 && to >= 0
      ? shown.slice(Math.min(from, to), Math.max(from, to) + 1).map((row) => row.part) : undefined;
    onSelect(part, selectionMode(event), range);
    if (!event.shiftKey) anchor.current = part;
  };
  return (
    <div className="assembly-parts">
      <input
        className="filter"
        placeholder={`Filter ${instances.length} parts`}
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
      />
      <ul ref={list} role="listbox" aria-label="Assembly parts" aria-multiselectable="true" tabIndex={0}
        onKeyDown={(event) => {
          if (event.target !== event.currentTarget) return;
          const at = shown.findIndex((row) => row.part === marked);
          let next: string | undefined;
          if (event.key === "ArrowDown") next = shown[Math.min(at + 1, shown.length - 1)]?.part;
          else if (event.key === "ArrowUp") next = shown[Math.max(at - 1, 0)]?.part;
          else if (event.key === "Home") next = shown[0]?.part;
          else if (event.key === "End") next = shown.at(-1)?.part;
          else if (event.key === " " || event.key === "Enter") next = shown[at]?.part;
          else return;
          event.preventDefault();
          if (next !== undefined) choose(next, event);
        }}>
        {shown.map(({ part, leaves }) => {
          const showing = leaves.filter((leaf) => !hidden.has(leaf)).length;
          const visible = showing > 0;
          const selected = leaves.length > 0 && leaves.every((leaf) => selectedPaths.has(leaf));
          return (
            <li key={part} role="presentation">
              <div
                className={selected ? "row assembly-part selected" : "row assembly-part"}
                role="option"
                aria-selected={selected}
                data-part={part}
                onClick={(event) => { list.current?.focus(); choose(part, event); }}
                onDoubleClick={() => onOpen(part)}
              >
                <button
                  className={showing === leaves.length ? "eye" : visible ? "eye partial" : "eye off"}
                  title={visible ? `Hide its ${leaves.length} instance(s)` : `Show its ${leaves.length} instance(s)`}
                  onClick={(event) => {
                    event.stopPropagation();
                    onVisibility(leaves, !visible);
                  }}
                >
                  <Icon name={visible ? "eye" : "eye-off"} />
                </button>
                <span className={visible ? "title" : "title muted"}>{part}</span>
                <span className="count">×{leaves.length}</span>
                <button
                  className="open-part"
                  title={`Open ${part}: its features, sketches and source`}
                  onClick={(event) => {
                    event.stopPropagation();
                    onOpen(part);
                  }}
                >
                  Open
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
