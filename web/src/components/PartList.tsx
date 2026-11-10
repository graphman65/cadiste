import { useEffect, useMemo, useRef, useState } from "react";

import type { Load, Target } from "../App";
import type { AssemblyData, Project, TraceData } from "../types";
import { usePersistentSet } from "../persist";
import { Icon } from "./Icon";

interface Props {
  parts: string[];
  /** Part name -> its folder, level by level; a part without one is listed at the top. */
  folders: Project["folders"];
  /** The project's assemblies, by name, listed first. */
  assemblies: string[];
  target: Target | null;
  traces: Record<string, Load<TraceData>>;
  assemblyLoads: Record<string, Load<AssemblyData>>;
  /** Every file opened whole, by folder path. */
  files: Record<string, Load<AssemblyData>>;
  /** Parts owning the current composite selection. Reveal the last one's row and folders. */
  marked: readonly string[];
  onSelect: (target: Target) => void;
}

interface Folder {
  /** "chassis" or "chassis/drive_module": unique, the key its open state is kept under. */
  path: string;
  name: string;
  /** A file's level (a package with several files has one per file), not a package's. */
  file: boolean;
  folders: Folder[];
  parts: string[];
}

/** The folder tree, folders and parts in the order the project lists them. */
function folderTree(parts: string[], folders: Project["folders"]): Folder {
  const root: Folder = { path: "", name: "", file: false, folders: [], parts: [] };
  for (const part of parts) {
    let at = root;
    for (const { name, file } of folders[part] ?? []) {
      const path = at.path ? `${at.path}/${name}` : name;
      let next = at.folders.find((folder) => folder.path === path);
      if (next === undefined) {
        next = { path, name, file, folders: [], parts: [] };
        at.folders.push(next);
      }
      at = next;
    }
    at.parts.push(part);
  }
  return root;
}

function count(folder: Folder): number {
  return folder.parts.length + folder.folders.reduce((sum, child) => sum + count(child), 0);
}

function Status({ state, done }: { state: Load<unknown> | undefined; done: string }) {
  if (state?.status === "loading") return <span className="spinner" title="Building…" />;
  if (state?.status === "error") return <span className="part-status error">failed</span>;
  if (state?.status === "ok") return <span className="part-status">{done}</span>;
  return null;
}

/** px per nesting level. */
const INDENT = 20;
/** A folder row's left padding at the top level: its chevron, then its icon. */
const FOLDER_INDENT = 6;
/** A part's left padding at the top level: past where a folder's chevron sits, so it reads as inside. */
const PART_INDENT = 26;

export function PartList(props: Props) {
  const { parts, folders, assemblies, target, traces, assemblyLoads, files, marked, onSelect } = props;
  const [filter, setFilter] = useState("");
  const [closed, toggleClosed] = usePersistentSet("cadiste.closed-folders");
  const needle = filter.trim().toLowerCase();
  const shown = useMemo(() => parts.filter((part) => part.toLowerCase().includes(needle)), [parts, needle]);
  const tree = useMemo(() => folderTree(shown, folders), [shown, folders]);
  const list = useRef<HTMLUListElement>(null);
  // The marked part still to scroll to: once the folders opened for it have rendered its row.
  const reveal = useRef<string | null>(null);

  // Reveal the last marked part only when the selection changes, so its folders can be closed again.
  useEffect(() => {
    const part = marked.at(-1) ?? null;
    reveal.current = part;
    if (part === null) return;
    let path = "";
    for (const { name } of folders[part] ?? []) {
      path = path ? `${path}/${name}` : name;
      if (closed.has(path)) toggleClosed(path);
    }
  }, [marked]);

  useEffect(() => {
    if (reveal.current === null) return;
    const row = list.current?.querySelector(`[data-part="${CSS.escape(reveal.current)}"]`);
    if (row == null) return;
    row.scrollIntoView({ block: "nearest" });
    reveal.current = null;
  });

  const renderPart = (part: string, depth: number) => {
    const state = traces[part];
    const selected = (target?.kind === "part" && target.name === part) || marked.includes(part);
    return (
      <li key={part}>
        <button
          className={selected ? "part selected" : "part"}
          style={{ paddingLeft: PART_INDENT + depth * INDENT }}
          data-part={part}
          onClick={() => onSelect({ kind: "part", name: part })}
        >
          <span className="part-name">{part}</span>
          <Status state={state} done={state?.status === "ok" ? `${state.data.ops.length} ops` : ""} />
        </button>
      </li>
    );
  };

  const renderFolder = (folder: Folder, depth: number) => {
    // While filtering, every folder holding a match is open.
    const open = needle !== "" || !closed.has(folder.path);
    const file = folder.file;
    const icon = file ? "file" : open ? "folder-open" : "folder";
    // Any folder opens whole: a file's parts, or a package's (every file of it).
    const viewing = target?.kind === "file" && target.path === folder.path;
    return (
      <li key={`folder:${folder.path}`}>
        <div className={viewing ? "folder-row selected" : "folder-row"}>
          <button
            className={file ? "part folder file" : "part folder"}
            style={{ paddingLeft: FOLDER_INDENT + depth * INDENT }}
            aria-expanded={open}
            title={open ? "Collapse" : "Expand"}
            onClick={() => toggleClosed(folder.path)}
          >
            <Icon name="chevron" className={open ? "chevron open" : "chevron"} />
            <Icon name={icon} className="group-icon" />
            <span className="part-name">{folder.name}</span>
            <Status state={files[folder.path]} done="" />
            <span className="part-status">{count(folder)}</span>
          </button>
          <button
            className="open-file"
            title={`View every part of ${folder.name} together${folder.folders.length > 0 ? ", from all its files" : ""}`}
            onClick={() => onSelect({ kind: "file", path: folder.path })}
          >
            Open
          </button>
        </div>
        {open && (
          // The guide line runs down from the folder's chevron.
          <ul className="folder-children" style={{ backgroundPositionX: FOLDER_INDENT + depth * INDENT + 6 }}>
            {renderEntries(folder, depth + 1)}
          </ul>
        )}
      </li>
    );
  };

  const renderEntries = (folder: Folder, depth: number) => [
    ...folder.folders.map((child) => renderFolder(child, depth)),
    ...folder.parts.map((part) => renderPart(part, depth)),
  ];

  return (
    <nav className="parts">
      {assemblies.map((name) => (
        <button
          key={name}
          className={target?.kind === "assembly" && target.name === name ? "part assembly selected" : "part assembly"}
          onClick={() => onSelect({ kind: "assembly", name })}
        >
          <Icon name="assembly" />
          <span className="part-name">{name}</span>
          <Status state={assemblyLoads[name]} done="" />
        </button>
      ))}
      <input
        className="filter"
        placeholder={`Filter ${parts.length} parts`}
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
      />
      <ul className="part-tree" ref={list}>
        {renderEntries(tree, 0)}
      </ul>
    </nav>
  );
}
