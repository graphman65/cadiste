import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  type Built,
  type BuildMode,
  createReference,
  fetchAssembly,
  fetchFile,
  fetchProject,
  fetchTrace,
  TraceFailure,
} from "./api";
import { AssemblyPane } from "./components/AssemblyPane";
import { CadView, type CadViewHandle } from "./components/CadView";
import { Details, HidePanelButton } from "./components/Details";
import { FeatureHover, InstanceHover } from "./components/HoverCard";
import { FeatureTree } from "./components/FeatureTree";
import { FileFeatures } from "./components/FileFeatures";
import { ExportMenu } from "./components/ExportMenu";
import { Icon } from "./components/Icon";
import { LinkRequests, LinkedSessions } from "./components/Link";
import { PartList } from "./components/PartList";
import { Splitter } from "./components/Splitter";
import { Timeline } from "./components/Timeline";
import type { SavedViewState } from "./components/SavedViews";
import { ViewBar } from "./components/ViewBar";
import { useHub } from "./hub";
import {
  ancestors,
  assemblyFocus,
  fileFocus,
  findShape,
  leafFaceCount,
  leafOfPart,
  leafOfPick,
  nodeForPick,
  opNodeId,
  pathInPart,
  sceneFocus,
  shapeLeaves,
  treeIndex,
  type TreeIndex,
} from "./model";
import { usePersistentFlag, usePersistentNumber } from "./persist";
import { updateSelection, type SelectionMode } from "./selection";
import type { AssemblyData, HubCalls, HubRequest, Project, SceneData, SelectionItem, ShapeNode, TabState, TraceData } from "./types";

const NO_NODES: ReadonlySet<string> = new Set();

/** Whether a part's folder is `path` or under it. */
function inFolder(project: Project, part: string, path: string): boolean {
  const folder = (project.folders[part] ?? []).map((step) => step.name).join("/");
  return folder === path || folder.startsWith(`${path}/`);
}

/** The parts in a folder and the folders under it: a file's ("chassis/drive_module"), or a package's ("chassis"). */
function folderParts(project: Project | null, path: string): string[] {
  return project === null ? [] : project.parts.filter((part) => inFolder(project, part, path));
}

/** A part's trace and its leaf in a file's scene, when the leaf has the trace's faces (the same build of it). */
function partInFile(data: SceneData, traces: Record<string, Load<TraceData>>, part: string) {
  const load = traces[part];
  const leaf = leafOfPart(data.shapes, part);
  if (load?.status !== "ok" || leaf === null) return null;
  return leafFaceCount(data, leaf) === load.data.result.faces ? { trace: load.data, leaf } : null;
}

/**
 * What is open: a part (traced), an assembly, or every part of a folder, both viewed: a file's
 * ("chassis/drive_module") or a package's ("chassis", all its files). An assembly's `name` is "" (the first one) until
 * the project is known: a `#assembly` link.
 */
export type Target = { kind: "part"; name: string } | { kind: "assembly"; name: string } | { kind: "file"; path: string };

export type Load<T> =
  | { status: "loading" }
  /** `cached`: the server had it, built for the model's current source; `shapes`: the file to measure on. */
  | { status: "ok"; data: T; cached: boolean; shapes: string | null }
  | { status: "error"; error: string; log?: string };

function targetFromHash(): Target | null {
  const hash = decodeURIComponent(window.location.hash.slice(1));
  if (hash === "assembly") return { kind: "assembly", name: "" };
  if (hash.startsWith("assembly=")) return { kind: "assembly", name: hash.slice("assembly=".length) };
  if (hash.startsWith("part=")) return { kind: "part", name: hash.slice("part=".length) };
  if (hash.startsWith("file=")) return { kind: "file", path: hash.slice("file=".length) };
  return null;
}

function hashOf(target: Target): string {
  if (target.kind === "assembly") return `#assembly=${encodeURIComponent(target.name)}`;
  if (target.kind === "file") return `#file=${encodeURIComponent(target.path)}`;
  return `#part=${encodeURIComponent(target.name)}`;
}

/** The key a target's build is asked for (and kept) under. */
function keyOf(target: Target): string {
  if (target.kind === "assembly") return `#assembly:${target.name}`;
  if (target.kind === "file") return `#file:${target.path}`;
  return target.name;
}

function failed(error: unknown): { status: "error"; error: string; log?: string } {
  const failure = error instanceof TraceFailure ? error : new TraceFailure(String(error));
  return { status: "error", error: failure.message, log: failure.log };
}

/**
 * The clipboard API needs a secure context (localhost is one) and may still refuse (a page without focus, a browser's
 * permission policy): then the old select-and-copy way.
 */
async function copyText(text: string): Promise<void> {
  if (window.isSecureContext && navigator.clipboard !== undefined) {
    try {
      return await navigator.clipboard.writeText(text);
    } catch {
      // refused: try the other way
    }
  }
  const area = document.createElement("textarea");
  area.value = text;
  document.body.append(area);
  area.select();
  const copied = document.execCommand("copy");
  area.remove();
  if (!copied) throw new Error("the browser refused to copy");
}

/** A load's outcome: the scene to render, or why there is none. */
type Loaded = { status: "ok"; scene: SceneData } | { status: "error"; error: string; log?: string };

/** Puts a fetched build (or its failure) where it is kept, and tells what came of it. */
function settle<T extends { scene: { data: SceneData } }>(
  fetched: Promise<Built<T>>,
  put: (load: Load<T>) => void,
): Promise<Loaded> {
  return fetched.then(
    (built): Loaded => {
      put({ status: "ok", ...built });
      return { status: "ok", scene: built.data.scene.data };
    },
    (error: unknown): Loaded => {
      const failure = failed(error);
      put(failure);
      return failure;
    },
  );
}

/** The features to light up next to the selection: what an operation consumed, what consumed a builder. */
function relatedTo(trace: TraceData, index: TreeIndex, selected: string | null): Set<string> {
  const node = selected === null ? undefined : index.nodes.get(selected);
  const related = new Set<string>();
  if (node?.type === "op") trace.ops[node.op].inputs.forEach((builder) => related.add(`b${builder}`));
  if (node?.type === "builder") {
    trace.builders[node.builder].consumed_by.forEach((op) => related.add(opNodeId(trace, op)));
  }
  return related;
}

/** Part instances in a tessellated assembly: the leaves of its shape tree. */
function countLeaves(node: ShapeNode): number {
  return node.parts === undefined ? 1 : node.parts.reduce((sum, part) => sum + countLeaves(part), 0);
}
/** Owning parts of composite selections; file-scene leaf names identify their parts directly. */
function assemblySelectionParts(data: AssemblyData, selection: SelectionItem[], fileNames = false): string[] {
  const root = data.scene.data.shapes;
  const partOf = { ...(data.scene.part_of ?? {}) };
  if (fileNames) for (const leaf of shapeLeaves(root)) partOf[leaf.id] ??= leaf.name;
  const leaves = selection.length === 0 ? shapeLeaves(root).map((leaf) => leaf.id) : selection.flatMap((item) => {
    if (item.kind === "entity") {
      const leaf = leafOfPick(root, item.path);
      return leaf === null ? [] : [leaf];
    }
    if (item.kind !== "instance") return [];
    const node = findShape(root, item.path);
    return node === undefined ? [] : shapeLeaves(node).map((leaf) => leaf.id);
  });
  return [...new Set([
    ...selection.flatMap((item) => item.kind === "feature" ? [item.part] : []),
    ...leaves.flatMap((leaf) => partOf[leaf] === undefined ? [] : [partOf[leaf]]),
  ])];
}


export function App() {
  const [project, setProject] = useState<Project | null>(null);
  const [projectError, setProjectError] = useState<string | null>(null);
  const [target, setTarget] = useState<Target | null>(targetFromHash);
  const [savedView, setSavedView] = useState<SavedViewState | null>(null);
  const [traces, setTraces] = useState<Record<string, Load<TraceData>>>({});
  const [assemblies, setAssemblies] = useState<Record<string, Load<AssemblyData>>>({});
  const [files, setFiles] = useState<Record<string, Load<AssemblyData>>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [selection, setSelection] = useState<SelectionItem[]>([]);
  // Fold state of each part's feature tree (a file shows several), by part name.
  const [collapsedBy, setCollapsedBy] = useState<Record<string, ReadonlySet<string>>>({});
  // In a file's view, the part whose feature is selected.
  const [focusPart, setFocusPart] = useState<string | null>(null);
  const [hiddenInstances, setHiddenInstances] = useState<ReadonlySet<string>>(new Set());
  const [instance, setInstance] = useState<string | null>(null);
  // In an assembly's view, the part whose instances are all lit.
  const [litPart, setLitPart] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [sidebarWidth, setSidebarWidth] = usePersistentNumber("cadiste.sidebar-width", 320);
  const [partsHeight, setPartsHeight] = usePersistentNumber("cadiste.parts-height", 240);
  const [panelWidth, setPanelWidth] = usePersistentNumber("cadiste.panel-width", 380);
  const [panelOpen, setPanelOpen] = usePersistentFlag("cadiste.panel-open", true);
  const hidePanel = useCallback(() => setPanelOpen(false), [setPanelOpen]);

  useEffect(() => {
    fetchProject()
      .then((loaded) => {
        setProject(loaded);
        setTarget((current) => {
          if (current?.kind === "assembly" && loaded.assemblies.includes(current.name)) return current;
          if (current?.kind === "part" && loaded.parts.includes(current.name)) return current;
          if (current?.kind === "file" && loaded.parts.some((part) => inFolder(loaded, part, current.path))) {
            return current;
          }
          if (loaded.assemblies.length > 0) return { kind: "assembly", name: loaded.assemblies[0] };
          return loaded.parts.length > 0 ? { kind: "part", name: loaded.parts[0] } : null;
        });
      })
      .catch((error: unknown) => setProjectError(String(error)));
  }, []);

  // Targets asked for once already: selecting one again shows what it has; Rebuild asks again.
  const requested = useRef(new Set<string>());
  const traceLoads = useRef(new Map<string, Promise<Loaded>>());
  const load = useCallback((what: Target, mode: BuildMode = "cached"): Promise<Loaded> => {
    requested.current.add(keyOf(what));
    if (what.kind === "assembly") {
      const name = what.name;
      setAssemblies((all) => ({ ...all, [name]: { status: "loading" } }));
      return settle(fetchAssembly(name, mode), (load) => setAssemblies((all) => ({ ...all, [name]: load })));
    }
    if (what.kind === "file") {
      const path = what.path;
      setFiles((all) => ({ ...all, [path]: { status: "loading" } }));
      return settle(fetchFile(path, mode), (load) => setFiles((all) => ({ ...all, [path]: load })));
    }
    const name = what.name;
    const pending = traceLoads.current.get(name);
    if (pending !== undefined && mode === "cached") return pending;
    setTraces((all) => ({ ...all, [name]: { status: "loading" } }));
    const loading = settle(fetchTrace(name, mode), (load) => setTraces((all) => ({ ...all, [name]: load })));
    traceLoads.current.set(name, loading);
    void loading.finally(() => {
      if (traceLoads.current.get(name) === loading) traceLoads.current.delete(name);
    });
    return loading;
  }, []);
  const view = useRef<CadViewHandle>(null);
  const clearSelection = useCallback(() => {
    setSelection([]);
    setSelected(null);
    setFocusPart(null);
    setInstance(null);
    setLitPart(null);
  }, []);

  useEffect(() => {
    if (target === null || (target.kind === "assembly" && target.name === "")) return;
    window.history.replaceState(null, "", hashOf(target));
    setSelected(null);
    setSelection([]);
    setCollapsedBy({});
    setFocusPart(null);
    setHover(null);
    setHiddenInstances(new Set());
    setInstance(null);
    setLitPart(null);
    view.current?.highlight([]);
    if (!requested.current.has(keyOf(target))) load(target);
  }, [target, load]);

  const fileParts = useMemo(
    () => (target?.kind === "file" ? folderParts(project, target.path) : []),
    [project, target],
  );
  useEffect(() => {
    // A file's view lists every part's features: their traces, cached once made.
    for (const name of fileParts) if (!requested.current.has(name)) load({ kind: "part", name });
  }, [fileParts, load]);

  const activePart = target?.kind === "part" ? target.name : target?.kind === "file" ? focusPart : null;
  const partLoad = activePart === null ? undefined : traces[activePart];
  // The assembly and a file's parts are both a group of shapes, viewed with the instance tree.
  const compositeLoad =
    target?.kind === "assembly" ? assemblies[target.name] : target?.kind === "file" ? files[target.path] : undefined;
  const state = target?.kind === "part" ? partLoad : compositeLoad;
  const trace = partLoad?.status === "ok" ? partLoad.data : null;
  const assemblyData = compositeLoad?.status === "ok" ? compositeLoad.data : null;
  const cached = state?.status === "ok" && state.cached;
  const shapes = state?.status === "ok" ? state.shapes : null;
  const assemblyParts = useMemo(
    () => target?.kind === "assembly" && assemblyData !== null && selection.length > 0
      ? assemblySelectionParts(assemblyData, selection) : [],
    [target, assemblyData, selection],
  );
  useEffect(() => {
    for (const name of assemblyParts) if (!requested.current.has(name)) void load({ kind: "part", name });
  }, [assemblyParts, load]);
  const index = trace === null ? null : treeIndex(trace);
  const collapsed = (activePart !== null && collapsedBy[activePart]) || NO_NODES;
  const related = useMemo(
    () => (trace === null || index === null ? new Set<string>() : relatedTo(trace, index, selected)),
    [trace, index, selected],
  );
  const focus = useMemo(() => {
    if (target?.kind === "part" && trace !== null && index !== null) {
      const features = selection.filter((item): item is Extract<SelectionItem, { kind: "feature" }> => item.kind === "feature" && item.part === target.name);
      const base = sceneFocus(trace, index, null);
      const focuses = features.map((item) => sceneFocus(trace, index, item.node));
      if (selection.length === 1 && focuses.length === 1) return focuses[0];
      const states = { ...base.states };
      for (const own of focuses) {
        for (const [path, state] of Object.entries(own.states)) {
          const previous = states[path] ?? [0, 0];
          states[path] = [previous[0] > state[0] ? previous[0] : state[0], previous[1] > state[1] ? previous[1] : state[1]];
        }
      }
      const entities = selection.flatMap((item) => item.kind === "entity" ? [item.path] : []);
      // Entity picks retain their exact path; maker focus is only for the Details panel.
      for (const path of entities) {
        const maker = nodeForPick(trace, path);
        if (maker === null) continue;
        const own = sceneFocus(trace, index, maker);
        for (const [leaf, state] of Object.entries(own.states)) {
          const previous = states[leaf] ?? [0, 0];
          states[leaf] = [previous[0] > state[0] ? previous[0] : state[0], previous[1] > state[1] ? previous[1] : state[1]];
        }
      }
      return {
        states,
        lit: [...new Set([...focuses.flatMap((own) => own.lit), ...entities])],
        ghost: focuses.some((own) => own.ghost),
        view: focuses.at(-1)?.view ?? null,
      };
    }
    if (assemblyData === null) return null;
    const root = assemblyData.scene.data.shapes;
    const base = assemblyFocus(root, hiddenInstances, null);
    const lit = selection.flatMap((item) => {
      if (item.kind === "entity") return [item.path];
      if (item.kind === "instance") {
        const node = findShape(root, item.path);
        return node === undefined ? [] : shapeLeaves(node).map((leaf) => leaf.id);
      }
      const inFile = target?.kind === "file" ? partInFile(assemblyData.scene.data, traces, item.part) : null;
      return inFile === null ? [] : fileFocus(root, hiddenInstances, { ...inFile, selected: item.node }).lit;
    });
    return { ...base, lit: [...new Set(lit)] };
  }, [target, trace, index, selection, assemblyData, traces, hiddenInstances]);

  // The target effect resets its selection, and a cold target may still be building. Restore only once its
  // scene is available; viewer-owned state then lands after the restored feature focus in the next commit.
  useEffect(() => {
    if (savedView === null) return;
    if (target === null || keyOf(target) !== keyOf(savedView.target) || state?.status === "error") {
      setSavedView(null);
      return;
    }
    if (state?.status !== "ok") return;
    const saved = savedView.scene;
    setHiddenInstances(new Set(saved.hidden));
    setSelection(saved.selection);
    setSelected(saved.selected);
    setFocusPart(saved.focusPart);
    setInstance(saved.instance);
    setLitPart(saved.litPart);
    setHover(null);
    view.current?.restore(savedView);
    setSavedView(null);
  }, [savedView, target, state]);

  /** Select a feature of a part (in a file's view, any of its parts), opening the folds above it. */
  const selectIn = useCallback(
    (part: string | null, id: string | null, mode: SelectionMode = "replace", range?: string[]) => {
      setFocusPart(part);
      setSelected(id);
      const items: SelectionItem[] = part === null || id === null ? [] : (range ?? [id]).map((node) => ({ kind: "feature", part, node }));
      setSelection((current) => updateSelection(current, items, mode));
      const load = part === null ? undefined : traces[part];
      if (part === null || id === null || load?.status !== "ok") return;
      const hidden = ancestors(treeIndex(load.data), id);
      setCollapsedBy((all) => {
        const current = all[part] ?? NO_NODES;
        if (!hidden.some((ancestor) => current.has(ancestor))) return all;
        const next = new Set(current);
        hidden.forEach((ancestor) => next.delete(ancestor));
        return { ...all, [part]: next };
      });
    },
    [traces],
  );
  const select = useCallback((id: string | null, mode?: SelectionMode, range?: string[]) => selectIn(activePart, id, mode, range), [selectIn, activePart]);

  /** In a file's view: the part a picked path is in, its trace, and the path as the part's own scene has it. */
  const pickInFile = useCallback(
    (path: string) => {
      if (target?.kind !== "file" || assemblyData === null) return null;
      const leaf = leafOfPick(assemblyData.scene.data.shapes, path);
      const part = shapeLeaves(assemblyData.scene.data.shapes).find((candidate) => candidate.id === leaf)?.name;
      const inFile = part === undefined ? null : partInFile(assemblyData.scene.data, traces, part);
      if (part === undefined || inFile === null) return null;
      return { part, trace: inFile.trace, path: pathInPart(inFile.trace, inFile.leaf, path) };
    },
    [target, assemblyData, traces],
  );

  const onPick = useCallback(
    (path: string, mode: SelectionMode = "replace") => {
      setSelection((current) => updateSelection(current, [{ kind: "entity", path }], mode));
      if (target?.kind === "part") {
        setFocusPart(target.name);
        setSelected(trace === null ? null : nodeForPick(trace, path));
        return;
      }
      const inFile = pickInFile(path);
      setFocusPart(inFile?.part ?? null);
      setSelected(inFile === null ? null : nodeForPick(inFile.trace, inFile.path));
      setInstance(assemblyData === null ? null : leafOfPick(assemblyData.scene.data.shapes, path));
      setLitPart(null);
    },
    [target, trace, assemblyData, pickInFile],
  );

  const describeHover = useCallback(
    (path: string) => {
      if (target?.kind === "part") {
        if (trace === null || index === null) return null;
        const node = nodeForPick(trace, path);
        return node === null ? null : <FeatureHover trace={trace} index={index} node={node} />;
      }
      const inFile = pickInFile(path);
      if (inFile !== null) {
        const node = nodeForPick(inFile.trace, inFile.path);
        return node === null ? null : <FeatureHover trace={inFile.trace} index={treeIndex(inFile.trace)} node={node} />;
      }
      if (assemblyData === null) return null;
      const leaf = leafOfPick(assemblyData.scene.data.shapes, path);
      const found = shapeLeaves(assemblyData.scene.data.shapes).find((candidate) => candidate.id === leaf);
      return found === undefined ? null : <InstanceHover leaf={found} />;
    },
    [target, trace, index, assemblyData, pickInFile],
  );

  const setVisibility = useCallback((leaves: string[], visible: boolean) => {
    setHiddenInstances((current) => {
      const next = new Set(current);
      leaves.forEach((leaf) => (visible ? next.delete(leaf) : next.add(leaf)));
      return next;
    });
  }, []);

  const toggleIn = useCallback((part: string, id: string, open?: boolean) => {
    setCollapsedBy((all) => {
      const next = new Set(all[part] ?? NO_NODES);
      const shouldOpen = open ?? next.has(id);
      if (shouldOpen) next.delete(id);
      else next.add(id);
      return { ...all, [part]: next };
    });
  }, []);
  const toggle = useCallback(
    (id: string, open?: boolean) => {
      if (activePart !== null) toggleIn(activePart, id, open);
    },
    [toggleIn, activePart],
  );

  const setCollapsed = (nodes: ReadonlySet<string>) => {
    if (activePart !== null) setCollapsedBy((all) => ({ ...all, [activePart]: nodes }));
  };
  const collapseAll = () => {
    if (index === null) return;
    const inner = [...index.nodes.values()].filter((node) => node.type !== "op" && index.parent.has(node.id));
    setCollapsed(new Set(inner.map((node) => node.id)));
  };

  /** In a file's view, each part's leaf: its visibility is the part's. */
  const leafOf = (part: string) => (assemblyData === null ? null : leafOfPart(assemblyData.scene.data.shapes, part));
  // In a file's view, the parts its eyes leave shown, when some are hidden: the ones Export writes.
  const shownParts = fileParts.filter((part) => {
    const leaf = leafOf(part);
    return leaf === null || !hiddenInstances.has(leaf);
  });
  const exportShown = shownParts.length < fileParts.length ? { parts: shownParts, of: fileParts.length } : null;

  // A call waits until what it changed is committed and the view's effects ran: the next render's effects.
  const committing = useRef<(() => void)[]>([]);
  const [, setCommits] = useState(0);
  useEffect(() => committing.current.splice(0).forEach((resolve) => resolve()));
  const afterCommit = useCallback(
    () =>
      new Promise<void>((resolve) => {
        committing.current.push(resolve);
        setCommits((count) => count + 1);
      }),
    [],
  );

  // The hub's calls read these: this render's, still current once a call has waited for a load or a render.
  const live = useRef({ target, index, assemblyData, selectIn, onPick, project, traces, selection });
  live.current = { target, index, assemblyData, selectIn, onPick, project, traces, selection };

  const handleHub = useCallback(
    async (request: HubRequest): Promise<unknown> => {
      const viewer = () => {
        if (view.current === null) throw new Error("Nothing is rendered");
        return view.current;
      };
      /** Sets the selection a `select` call (or `open`) asks for, once all of it is known to exist. */
      const applySelect = ({ items }: HubCalls["select"]["params"]) => {
        const { target: current, assemblyData: composite, traces: loadedTraces } = live.current;
        if (!Array.isArray(items)) throw new Error("Selection items must be an array");
        for (const item of items) {
          if (item.kind === "feature") {
            const allowed = current?.kind === "part" ? current.name === item.part
              : current?.kind === "file" && folderParts(live.current.project, current.path).includes(item.part);
            const load = loadedTraces[item.part];
            if (!allowed || load?.status !== "ok" || !treeIndex(load.data).nodes.has(item.node)) {
              throw new Error(`Unknown feature: ${item.part} / ${item.node}`);
            }
          } else if (item.kind === "instance") {
            if (composite === null || findShape(composite.scene.data.shapes, item.path) === undefined) {
              throw new Error(`Unknown instance: ${item.path}`);
            }
          } else if (item.kind === "entity") {
            const unknown = viewer().unknownPaths([item.path]);
            if (unknown.length > 0) throw new Error(`Unknown viewer paths: ${unknown.join(", ")}`);
          } else throw new Error("Unknown selection kind");
        }
        const next = updateSelection([], items, "replace");
        clearSelection();
        const last = next.at(-1);
        if (last?.kind === "feature") live.current.selectIn(last.part, last.node);
        else if (last?.kind === "instance") setInstance(last.path);
        else if (last?.kind === "entity") live.current.onPick(last.path);
        setSelection(next);
      };

      switch (request.method) {
        case "open": {
          const asked = (request.params as HubCalls["open"]["params"]).target;
          const chosen = (request.params as HubCalls["open"]["params"]).select;
          if (!["part", "assembly", "file"].includes(asked.kind)) {
            throw new Error(`Unknown target: ${JSON.stringify(asked)}`);
          }
          // An assembly asked for without its name: the first one.
          const first = live.current.project?.assemblies[0];
          const next: Target =
            asked.kind === "assembly" && !asked.name
              ? { kind: "assembly", name: first ?? "" }
              : asked;
          // Asked for before the target is set, so its effect does not ask for the cached build too.
          const loading = load(next, "latest");
          setTarget(next);
          const loaded = await loading;
          if (loaded.status === "error") {
            await afterCommit();
            return loaded;
          }
          await viewer().rendered(loaded.scene);
          if (chosen !== undefined && chosen !== null) {
            applySelect({ items: [next.kind === "part" ? { kind: "feature", part: next.name, node: chosen } : { kind: "instance", path: chosen }] });
          }
          await afterCommit();
          return { status: "ok" } satisfies HubCalls["open"]["result"];
        }
        case "select": {
          applySelect(request.params as HubCalls["select"]["params"]);
          await afterCommit();
          const current = live.current;
          if (current.target?.kind === "assembly" && current.assemblyData !== null) {
            const parts = assemblySelectionParts(current.assemblyData, current.selection);
            await Promise.all(parts.flatMap((part) => {
              const pending = traceLoads.current.get(part);
              if (pending !== undefined) return [pending];
              return current.traces[part] === undefined ? [load({ kind: "part", name: part })] : [];
            }));
            await afterCommit();
          }
          return { selection: live.current.selection };
        }
        case "highlight": {
          const { paths } = request.params as HubCalls["highlight"]["params"];
          const unknown = viewer().highlight(paths);
          if (unknown.length > 0) throw new Error(`Unknown viewer paths: ${unknown.join(", ")}`);
          await afterCommit();
          return {};
        }
        case "view":
          viewer().view(request.params as HubCalls["view"]["params"]);
          await afterCommit();
          return {};
        case "visibility": {
          const { instances, visible } = request.params as HubCalls["visibility"]["params"];
          const composite = live.current.assemblyData;
          if (composite === null) throw new Error("Only the assembly's or a file's view has instances");
          const nodes = instances.map((id) => findShape(composite.scene.data.shapes, id));
          const unknown = instances.filter((_, i) => nodes[i] === undefined);
          if (unknown.length > 0) throw new Error(`Unknown instances: ${unknown.join(", ")}`);
          // A group's eye is its instances'.
          setVisibility(
            nodes.flatMap((node) => (node === undefined ? [] : shapeLeaves(node).map((leaf) => leaf.id))),
            visible,
          );
          await afterCommit();
          return {};
        }
        case "screenshot": {
          const { views, width } = request.params as HubCalls["screenshot"]["params"];
          return { images: await viewer().screenshot(views, width) } satisfies HubCalls["screenshot"]["result"];
        }
        default:
          throw new Error(`Unknown method: ${request.method}`);
      }
    },
    [load, afterCommit, setVisibility, clearSelection],
  );

  const tabState: TabState = {
    target,
    status: state?.status ?? "none",
    error: state?.status === "error" ? state.error : null,
    shapes,
    selection,
    partShapes: Object.fromEntries(Object.entries(traces).flatMap(([part, load]) =>
      load.status === "ok" && load.shapes !== null ? [[part, load.shapes]] : [])),
    hidden: [...hiddenInstances],
  };
  const hub = useHub(tabState, handleHub);

  // ---- standalone snapshots: explicit selection first; pointed context only when it is empty ----

  const [notice, setNotice] = useState<{ text: string; failed: boolean } | null>(null);
  useEffect(() => {
    if (notice === null) return;
    const timer = window.setTimeout(() => setNotice(null), 4000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  /** The element under the pointer, for the shortcut: what it points at is what it refers to. */
  const pointed = useRef<Element | null>(null);
  useEffect(() => {
    const onOver = (event: PointerEvent) => {
      pointed.current = event.target instanceof Element ? event.target : null;
    };
    document.addEventListener("pointerover", onOver);
    return () => document.removeEventListener("pointerover", onOver);
  }, []);

  /** Pointed context is a convenience only when there is no explicit selection to export. */
  const referred = (): TabState => {
    if (selection.length > 0) return tabState;
    const under = pointed.current;
    const row = under?.closest<HTMLElement>(".sidebar .tree [data-id]");
    const part = row?.closest<HTMLElement>("[data-part]")?.dataset.part;
    if (part !== undefined && row?.dataset.id !== undefined) {
      return { ...tabState, selection: [{ kind: "feature", part, node: row.dataset.id }] };
    }
    if (row?.dataset.id !== undefined && under?.closest(".assembly-tree")) {
      return { ...tabState, selection: [{ kind: "instance", path: row.dataset.id }] };
    }
    const assemblyPart = under?.closest<HTMLElement>(".assembly-part[data-part]")?.dataset.part;
    if (assemblyPart !== undefined && assemblyData !== null) {
      return { ...tabState, selection: Object.entries(assemblyData.scene.part_of ?? {}).flatMap(([path, part]) =>
        part === assemblyPart ? [{ kind: "instance" as const, path }] : []) };
    }
    const path = under?.closest(".cad-view") ? view.current?.hovered() : null;
    if (path) return { ...tabState, selection: [{ kind: "entity", path }] };
    if (hover !== null && target?.kind !== "part") return { ...tabState, selection: [{ kind: "instance", path: hover }] };
    return tabState;
  };

  const copyReference = async (state: TabState) => {
    try {
      if ((state.target?.kind === "assembly" || state.target?.kind === "file") && assemblyData !== null) {
        const parts = assemblySelectionParts(assemblyData, state.selection, state.target.kind === "file");
        const pending = parts.flatMap((part) => {
          const loading = traceLoads.current.get(part);
          if (loading !== undefined) return [loading];
          return live.current.traces[part] === undefined ? [load({ kind: "part", name: part })] : [];
        });
        if (pending.length > 0 || parts.some((part) => live.current.traces[part]?.status === "loading")) {
          setNotice({ text: "Loading selected parts’ feature context…", failed: false });
          await Promise.all(pending);
          await afterCommit();
        }
        const partShapes = { ...state.partShapes };
        for (const part of parts) {
          const loaded = live.current.traces[part];
          if (loaded?.status !== "ok" || loaded.shapes === null) {
            const reason = loaded?.status === "error" ? loaded.error : "trace build is unavailable";
            throw new Error(`Feature context for ${part} could not be loaded: ${reason}`);
          }
          partShapes[part] = loaded.shapes;
        }
        state = { ...state, partShapes };
      }
      const { text } = await createReference(hub.tab, state);
      await copyText(text);
      const label = state.selection.length === 0 ? "entire target" : `${state.selection.length} selected item(s)`;
      setNotice({ text: `Copied snapshot: ${label}`, failed: false });
    } catch (error) {
      setNotice({ text: `Could not copy snapshot: ${error instanceof Error ? error.message : String(error)}`, failed: true });
    }
  };
  // The key handler reads this render's state through a ref: it is attached once.
  const copyPointed = useRef(() => {});
  copyPointed.current = () => {
    if (target !== null && state?.status !== "loading") void copyReference(referred());
  };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.repeat) return;
      const typing = event.target instanceof HTMLElement && event.target.closest("input, textarea, select, [contenteditable], [role=dialog], .saved-views");
      if (typing) return;
      if (event.key === "Escape") {
        event.preventDefault();
        clearSelection();
        return;
      }
      if (event.key.toLowerCase() !== "c" || event.ctrlKey || event.metaKey || event.altKey) return;
      event.preventDefault();
      copyPointed.current();
    };
    // Captured: three-cad-viewer handles keys in its view.
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [clearSelection]);

  if (projectError !== null) {
    return <div className="fatal">Cannot reach the cadiste server: {projectError}</div>;
  }

  const title = target?.kind === "file" ? target.path.replaceAll("/", " / ") : target?.name;
  const sceneData = target?.kind === "part" ? (trace?.scene.data ?? null) : (assemblyData?.scene.data ?? null);

  const partView = target?.kind === "part";
  const assemblyView = target?.kind === "assembly";
  // In an assembly, the part shown selected in both lists: the one lit, or the part of the selected instance (a group
  // is of no one part).
  const markedPart =
    assemblyView && assemblyData !== null
      ? (litPart ?? (instance === null ? null : (assemblyData.scene.part_of?.[instance] ?? null)))
      : null;
  // A part, a file's parts or an assembly share the sidebar with what they list: features, parts and instances.
  const featurePane = target !== null;
  const showDetails = trace !== null && index !== null && (partView || selected !== null);

  // A hidden panel leaves only its show button: the column shrinks to it. An assembly's view has none: its
  // parts and instances are in the sidebar.
  const panelColumn = assemblyView ? "" : panelOpen ? ` ${panelWidth}px` : " auto";
  const layout = assemblyView ? "app assembly wide" : partView ? "app" : "app assembly";

  return (
    <div className={layout} style={{ gridTemplateColumns: `${sidebarWidth}px minmax(0, 1fr)${panelColumn}` }}>
      <header className="topbar">
        <span className="brand">cadiste</span>
        {project && <span className="project">{project.root}</span>}
        <span className="spacer" />
        <span className="selection-count" role="status" aria-live="polite" data-selection-count={selection.length}>
          {selection.length === 0 ? "Entire target" : `${selection.length} selected`}
        </span>
        <button className="clear-selection" disabled={selection.length === 0} onClick={clearSelection} title="Clear selection (Escape)">
          Clear selection
        </button>
        {trace && (
          <>
            <button onClick={() => setCollapsed(new Set())}>Expand all</button>
            <button onClick={collapseAll}>Collapse all</button>
          </>
        )}
        <LinkedSessions sessions={hub.sessions} tab={hub.tab} onUnlink={hub.unlink} />
        <button
          className="icon-button"
          aria-label="Copy standalone selection snapshot"
          data-action="copy-snapshot"
          disabled={target === null || state?.status === "loading"}
          onClick={() => void copyReference(tabState)}
          title="Copy a standalone snapshot of all selected items (no MCP lookup needed). C copies the selection; when empty, C uses pointed context. Ctrl/Cmd-click toggles; Shift-click adds; Escape clears."
        >
          <Icon name="links" />
        </button>
        <ExportMenu target={target} title={title} shown={exportShown} />
        <button
          className="primary"
          disabled={target === null || state?.status === "loading"}
          onClick={() => target && load(target, "fresh")}
          title="Build again, even if the cached build is up to date"
        >
          {state?.status === "loading" ? "Building…" : "Rebuild"}
        </button>
      </header>

      <aside className="sidebar">
        {/* The part list has the sidebar to itself until a part is open; then it shares it with the part's tree. */}
        <div
          className={featurePane ? "parts-pane" : "parts-pane full"}
          style={featurePane ? { height: partsHeight } : undefined}
        >
          <PartList
            parts={project?.parts ?? []}
            folders={project?.folders ?? {}}
            assemblies={project?.assemblies ?? []}
            target={target}
            traces={traces}
            assemblyLoads={assemblies}
            files={files}
            marked={markedPart}
            onSelect={setTarget}
          />
        </div>
        {featurePane && (
          <>
            <Splitter
              axis="y"
              value={partsHeight}
              min={90}
              max={Math.max(120, window.innerHeight - 260)}
              onChange={setPartsHeight}
            />
            <div className="sidebar-tree">
              {partView && trace && index && trace.roots.length > 0 && (
                <FeatureTree
                  trace={trace}
                  index={index}
                  collapsed={collapsed}
                  selected={selected}
                  part={target.name}
                  selectedNodes={new Set(selection.flatMap((item) => item.kind === "feature" && item.part === target.name ? [item.node] : []))}
                  related={related}
                  onSelect={select}
                  onToggle={toggle}
                />
              )}
              {partView && trace && trace.roots.length === 0 && (
                <div className="placeholder">
                  No builder operation ran: this {trace.result.type} was made without one, so it has no features.
                </div>
              )}
              {target?.kind === "file" && (
                <FileFeatures
                  parts={fileParts}
                  traces={traces}
                  selected={focusPart !== null && selected !== null ? { part: focusPart, node: selected } : null}
                  selection={selection}
                  related={related}
                  collapsed={collapsedBy}
                  visible={(part) => {
                    const leaf = leafOf(part);
                    return leaf === null || !hiddenInstances.has(leaf);
                  }}
                  onSelect={selectIn}
                  onToggle={toggleIn}
                  onVisibility={(part, visible) => {
                    const leaf = leafOf(part);
                    if (leaf !== null) setVisibility([leaf], visible);
                  }}
                  onHover={(part) => setHover(part === null ? null : leafOf(part))}
                  onOpen={(name) => setTarget({ kind: "part", name })}
                />
              )}
              {assemblyView && assemblyData && (
                <AssemblyPane
                  name={target.name}
                  data={assemblyData}
                  parts={project?.parts ?? []}
                  hidden={hiddenInstances}
                  instance={instance}
                  marked={markedPart}
                  selectedPaths={new Set(selection.flatMap((item) => item.kind === "instance" ? [item.path] : []))}
                  onVisibility={setVisibility}
                  onSelectInstance={(id, mode = "replace", range) => {
                    setInstance(id);
                    setLitPart(null);
                    setSelection((current) => updateSelection(current, (range ?? [id]).map((path) => ({ kind: "instance", path })), mode));
                  }}
                  onSelectPart={(part, mode = "replace", range) => {
                    setLitPart(part);
                    setInstance(null);
                    const parts = new Set(range ?? [part]);
                    const items: SelectionItem[] = Object.entries(assemblyData.scene.part_of ?? {})
                      .flatMap(([path, name]) => parts.has(name) ? [{ kind: "instance", path }] : []);
                    // A part row is one group action: toggling a partially selected row selects all its instances.
                    setSelection((current) => {
                      const removing = mode === "toggle" && items.every((item) => current.some((chosen) => chosen.kind === "instance" && item.kind === "instance" && chosen.path === item.path));
                      if (removing) return current.filter((chosen) => !items.some((item) => chosen.kind === "instance" && item.kind === "instance" && chosen.path === item.path));
                      return updateSelection(current, items, mode === "toggle" ? "add" : mode);
                    });
                  }}
                  onHover={setHover}
                  onOpen={(name) => setTarget({ kind: "part", name })}
                />
              )}
            </div>
          </>
        )}
        <Splitter axis="x" value={sidebarWidth} min={220} max={640} onChange={setSidebarWidth} />
      </aside>

      <main className="view">
        {target !== null && (
          <ViewBar target={target} project={project}>
            {partView && trace && (
              <span className="stats">
                {trace.ops.length} ops · {trace.result.attributed}/{trace.result.faces} faces traced · built in{" "}
                {trace.build_seconds.toFixed(2)} s{cached && " · from cache"}
              </span>
            )}
            {assemblyData && (
              <span className="stats">
                {countLeaves(assemblyData.scene.data.shapes)} {target.kind === "file" ? "parts" : "instances"} · built
                in {assemblyData.build_seconds.toFixed(2)} s{cached && " · from cache"}
              </span>
            )}
          </ViewBar>
        )}
        <div className="view-body">
          <CadView
            ref={view}
            data={sceneData}
            focus={focus}
            hover={hover}
            onPick={onPick}
            describe={describeHover}
            shapes={shapes}
            selectionPaths={selection.flatMap((item) => item.kind === "entity" ? [item.path] : [])}
            onDropPick={(path) => setSelection((current) => current.filter((item) => item.kind !== "entity" || item.path !== path))}
            onClearPicks={() => setSelection((current) => current.filter((item) => item.kind !== "entity"))}
            onClearSelection={clearSelection}
            viewScope={project === null ? null : JSON.stringify([project.root, project.target])}
            viewTarget={target}
            sceneState={{ hidden: [...hiddenInstances], selection, selected, focusPart, instance, litPart }}
            onRestoreView={(saved) => {
              setSavedView(saved);
              setTarget((current) => current !== null && keyOf(current) === keyOf(saved.target) ? current : saved.target);
            }}
          />
          {state?.status === "loading" && (
            <div className="view-overlay">
              <span className="spinner" /> Building {title}…
            </div>
          )}
          {state?.status === "error" && (
            <div className="view-overlay failure">
              <h2>{state.error}</h2>
              {state.log && <pre>{state.log}</pre>}
            </div>
          )}
        </div>
      </main>

      {assemblyView ? null : panelOpen ? (
        <div className="panel">
          <Splitter
            axis="x"
            reverse
            value={panelWidth}
            min={260}
            max={Math.max(300, window.innerWidth - sidebarWidth - 320)}
            onChange={setPanelWidth}
          />
          {showDetails && trace && index ? (
            <Details trace={trace} index={index} selected={selected} onSelect={select} onHide={hidePanel} />
          ) : (
            // A file's parts are listed in the sidebar: the panel waits for a feature of one of them.
            <aside className="details">
              <div className="details-bar">
                <span className="spacer" />
                <HidePanelButton onHide={hidePanel} />
              </div>
              {target?.kind === "file" && (
                <div className="empty">Select a feature of a part to see its arguments, inputs and source.</div>
              )}
            </aside>
          )}
        </div>
      ) : (
        <div className="panel hidden">
          <button className="icon-button" onClick={() => setPanelOpen(true)} title="Show the panel">
            <Icon name="panel-show" />
          </button>
        </div>
      )}
      {partView && trace && index ? (
        <Timeline trace={trace} index={index} selected={selected}
          selectedNodes={new Set(selection.flatMap((item) => item.kind === "feature" && item.part === target.name ? [item.node] : []))}
          onSelect={select} />
      ) : (
        partView && <footer className="timeline" />
      )}
      <LinkRequests sessions={hub.sessions} onLink={hub.link} onDecline={hub.decline} />
      {notice && <div className={notice.failed ? "notice failed" : "notice"}>{notice.text}</div>}
    </div>
  );
}
