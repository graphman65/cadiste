import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  type Built,
  type BuildMode,
  type BackgroundBuild,
  type BuildsSnapshot,
  createReference,
  fetchAssembly,
  fetchFile,
  fetchProject,
  fetchTrace,
  listenBuilds,
  fetchBaselineScene,
  type BaselineEntry,
  TraceFailure,
} from "./api";
import { AssemblyPane } from "./components/AssemblyPane";
import { CadView, type CadViewHandle } from "./components/CadView";
import { Details, HidePanelButton } from "./components/Details";
import { FeatureHover, InstanceHover } from "./components/HoverCard";
import { FeatureTree } from "./components/FeatureTree";
import { FileFeatures } from "./components/FileFeatures";
import { ExportMenu } from "./components/ExportMenu";
import { BaselineReview } from "./components/BaselineReview";
import { CheckReview, type ExactCheckReport } from "./components/CheckReview";
import { WorkflowTools } from "./components/WorkflowTools";
import { Icon } from "./components/Icon";
import { LinkRequests, LinkedSessions } from "./components/Link";
import { PartList } from "./components/PartList";
import { Splitter } from "./components/Splitter";
import { SimulationControls } from "./components/SimulationControls";
import { SnapshotCopy, type SnapshotCopyState } from "./components/SnapshotCopy";
import { Timeline } from "./components/Timeline";
import { ProjectLauncher } from "./components/ProjectLauncher";
import { RuntimeInfo } from "./components/RuntimeInfo";
import { SolidCheck } from "./components/SolidCheck";
import { editSavedViews, type SavedViewState } from "./components/SavedViews";
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
import { useLiveSimulation } from "./simulation";
import { updateSelection, type SelectionMode } from "./selection";
import type { AssemblyData, BuildIdentity, CaptureArrangement, HubCalls, HubRequest, Project, SceneData, SelectionItem, ShapeNode, TabState, TraceData } from "./types";

const NO_NODES: ReadonlySet<string> = new Set();
const CAPTURE_MODE = new URLSearchParams(window.location.search).get("capture") === "1";
const HARNESS_MODE = new URLSearchParams(window.location.search).get("harness") === "1";

declare global {
  interface Window {
    cadisteCapture?: { protocol: string; call: (request: HubRequest) => Promise<unknown>; inspect: () => Record<string, unknown> };
    cadisteRenderer?: { inspect: () => Record<string, unknown> };
  }
}

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
function partInFile(data: AssemblyData, traces: Record<string, Load<TraceData>>, part: string) {
  const load = traces[part];
  const leaf = leafOfPart(data.scene.data.shapes, part, data.scene.part_of);
  if (load?.status !== "ok" || leaf === null) return null;
  if (data.source_fingerprint !== undefined && load.data.source_fingerprint !== undefined &&
    data.source_fingerprint !== load.data.source_fingerprint) return null;
  return leafFaceCount(data.scene.data, leaf) === load.data.result.faces ? { trace: load.data, leaf } : null;
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
  | { status: "ok"; data: T; cached: boolean; shapes: string | null; refreshing?: boolean; failure?: { error: string; log?: string } }
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

/** Infer the catalog from any target, including a hash or a private capture, before using the default. */
function variantIn(project: Project, target: Target | null): string | null {
  const variants = project.variants ?? [];
  const identity = target?.kind === "file" ? target.path : target?.name;
  return variants.find(({ id }) => identity?.startsWith(`${id}/`) || target?.kind === "file" && identity === id)?.id
    ?? variants.find(({ id }) => id === project.default_variant)?.id
    ?? variants[0]?.id
    ?? null;
}

function catalogIn(project: Project, variant: string | null) {
  if (variant === null) return { parts: project.parts, assemblies: project.assemblies };
  const prefix = `${variant}/`;
  return {
    parts: project.parts.filter((name) => name.startsWith(prefix)),
    assemblies: project.assemblies.filter((name) => name.startsWith(prefix)),
  };
}

/** Keep an available target, otherwise stay within its inferred (or default) catalog. */
function targetIn(project: Project, current: Target | null, variant = variantIn(project, current)): Target | null {
  const catalog = catalogIn(project, variant);
  if (current?.kind === "assembly" && catalog.assemblies.includes(current.name)) return current;
  if (current?.kind === "part" && catalog.parts.includes(current.name)) return current;
  if (current?.kind === "file" && catalog.parts.some((part) => inFolder(project, part, current.path))) return current;
  if (catalog.assemblies.length > 0) return { kind: "assembly", name: catalog.assemblies[0] };
  return catalog.parts.length > 0 ? { kind: "part", name: catalog.parts[0] } : null;
}

function failed(error: unknown): { status: "error"; error: string; log?: string } {
  const failure = error instanceof TraceFailure ? error : new TraceFailure(String(error));
  return { status: "error", error: failure.message, log: failure.log };
}

/** A failed refresh must not take a usable scene (or its trace) away. */
function retain<T>(previous: Load<T> | undefined, next: Load<T>): Load<T> {
  if (previous?.status !== "ok") return next;
  if (next.status === "loading") return { ...previous, refreshing: true, failure: undefined };
  if (next.status === "error") return { ...previous, refreshing: false, failure: next };
  return next;
}

function stopLoading<T>(all: Record<string, Load<T>>, name: string): Record<string, Load<T>> {
  const previous = all[name];
  return previous?.status === "ok" && previous.refreshing
    ? { ...all, [name]: { ...previous, refreshing: false } } : all;
}



/** A load's outcome: the scene to render, or why there is none. */
type Loaded = { status: "ok"; scene: SceneData; build_id: string } | { status: "error"; error: string; log?: string };

/** Puts a fetched build (or its failure) where it is kept, and tells what came of it. */
function settle<T extends { build_id: string; scene: { data: SceneData } }>(
  fetched: Promise<Built<T>>,
  put: (load: Load<T>) => void,
): Promise<Loaded> {
  return fetched.then(
    (built): Loaded => {
      put({ status: "ok", ...built });
      return { status: "ok", scene: built.data.scene.data, build_id: built.data.build_id };
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

/** Adapter identities only: these nodes do not claim ownership by a traced CAD part. */
function SimulationSceneNode({ node }: { node: ShapeNode }) {
  return <li data-scene-node={node.id}><span>{node.name}</span>
    {node.parts !== undefined && <ul>{node.parts.map((child) => <SimulationSceneNode key={child.id} node={child} />)}</ul>}
  </li>;
}


export function App() {
  const [project, setProject] = useState<Project | null>(null);
  const [projectError, setProjectError] = useState<string | null>(null);
  const [refreshingProject, setRefreshingProject] = useState(false);
  const [target, setTarget] = useState<Target | null>(() => CAPTURE_MODE ? null : targetFromHash());
  const comparisonBusy = useRef(false);
  const [comparisonActive, setComparisonActive] = useState(false);
  const [sceneOverride, setSceneOverride] = useState<SceneData | null>(null);
  const [comparisonReview, setComparisonReview] = useState<{
    baseline: { id: string; name?: string; image: string; build_id?: string };
    current: { image: string; build_id?: string };
  } | null>(null);
  const [workflowReport, setWorkflowReport] = useState<Record<string, unknown> | null>(null);
  const downloadTimings = useRef(new Map<string, number>());
  const activeVariant = project === null ? null : variantIn(project, target);
  const catalog = useMemo(
    () => project === null ? { parts: [], assemblies: [] } : catalogIn(project, activeVariant),
    [project, activeVariant],
  );
  const { live: simulation, enable: enableSimulation, disable: disableSimulation } = useLiveSimulation(target?.kind === "assembly" ? target : null);
  const [savedView, setSavedView] = useState<SavedViewState | null>(null);
  const [assemblyReturn, setAssemblyReturn] = useState<{
    target: Extract<Target, { kind: "assembly" }>;
    view: SavedViewState | null;
  } | null>(null);
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
  // Each tab overrides only its own eyes; both visibility layers must allow a leaf.
  const [hiddenParts, setHiddenParts] = useState<ReadonlySet<string>>(new Set());
  const [soloPart, setSoloPart] = useState<string | null>(null);
  const [soloInstance, setSoloInstance] = useState<string | null>(null);
  const [visibilityTarget, setVisibilityTarget] = useState<string | null>(null);
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
        if (!CAPTURE_MODE && !loaded.workspace_only) setTarget((current) => targetIn(loaded, current));
      })
      .catch((error: unknown) => setProjectError(String(error)));
  }, []);

  // Only download generations of targets the browser needs, not every server-warmed scene.
  const requested = useRef(new Set<string>());
  const traceLoads = useRef(new Map<string, Promise<Loaded>>());
  const loadVersions = useRef(new Map<string, number>());
  const pendingLoads = useRef(new Map<string, BuildMode>());
  const loadedIds = useRef(new Map<string, string>());
  const obsoleteIds = useRef(new Map<string, Set<string>>());
  const readyBuilds = useRef(new Map<string, BackgroundBuild>());
  const attemptedIds = useRef(new Map<string, string>());
  const latestFingerprint = useRef("");
  const catalogFingerprint = useRef<string | null>(null);
  const reconcileBuilds = useRef<() => void>(() => {});
  const context = useRef({ target, project, simulation: simulation.enabled });
  context.current = { target, project, simulation: simulation.enabled };
  const pauseLoad = useCallback((what: Target) => {
    if (what.kind === "part") setTraces((all) => stopLoading(all, what.name));
    else if (what.kind === "assembly") setAssemblies((all) => stopLoading(all, what.name));
    else setFiles((all) => stopLoading(all, what.path));
  }, []);
  const [background, setBackground] = useState<BackgroundBuild | null>(null);
  const [watchError, setWatchError] = useState<string | null>(null);
  const [connection, setConnection] = useState<"connecting" | "connected" | "disconnected">("connecting");
  const load = useCallback((what: Target, mode: BuildMode = "cached"): Promise<Loaded> => {
    const startedAt = performance.now();
    if (what.kind === "part" && mode === "cached") {
      const pending = traceLoads.current.get(what.name);
      if (pending !== undefined) return pending;
    }
    const key = keyOf(what);
    const version = (loadVersions.current.get(key) ?? 0) + 1;
    loadVersions.current.set(key, version);
    pendingLoads.current.set(key, mode);
    requested.current.add(key);
    const startedFingerprint = latestFingerprint.current;
    const forget = (id: string | undefined) => {
      if (id === undefined) return;
      let ids = obsoleteIds.current.get(key);
      if (ids === undefined) obsoleteIds.current.set(key, ids = new Set());
      ids.add(id);
    };
    if (mode === "fresh") {
      forget(loadedIds.current.get(key));
      forget(readyBuilds.current.get(key)?.build_id);
    }
    const accept = (next: Load<TraceData | AssemblyData>) => {
      if (loadVersions.current.get(key) !== version) return false;
      pendingLoads.current.delete(key);
      // An exact download started in CAD may finish after simulation was enabled.
      if (typeof mode === "object" && context.current.simulation && !CAPTURE_MODE) {
        attemptedIds.current.delete(key);
        pauseLoad(what);
        return false;
      }
      if (next.status !== "ok") return true;
      const previous = loadedIds.current.get(key);
      const id = next.data.build_id;
      if (previous !== id) {
        forget(previous);
        const current = context.current.target;
        if (current !== null && (keyOf(current) === key || current.kind === "file" && what.kind === "part")) {
          setSelection([]);
          setSelected(null);
          setFocusPart(null);
          setInstance(null);
          setLitPart(null);
          setHover(null);
        }
      }
      if (mode === "fresh") {
        const advertised = readyBuilds.current.get(key)?.build_id;
        if (advertised !== id && (latestFingerprint.current === startedFingerprint ||
          next.data.source_fingerprint === latestFingerprint.current)) forget(advertised);
      }
      loadedIds.current.set(key, id);
      return true;
    };
    let loading: Promise<Loaded>;
    if (what.kind === "assembly") {
      const name = what.name;
      setAssemblies((all) => ({ ...all, [name]: retain(all[name], { status: "loading" }) }));
      loading = settle(fetchAssembly(name, mode), (next) => {
        if (accept(next)) setAssemblies((all) => ({ ...all, [name]: retain(all[name], next) }));
      });
    } else if (what.kind === "file") {
      const path = what.path;
      setFiles((all) => ({ ...all, [path]: retain(all[path], { status: "loading" }) }));
      loading = settle(fetchFile(path, mode), (next) => {
        if (accept(next)) setFiles((all) => ({ ...all, [path]: retain(all[path], next) }));
      });
    } else {
      const name = what.name;
      setTraces((all) => ({ ...all, [name]: retain(all[name], { status: "loading" }) }));
      loading = settle(fetchTrace(name, mode), (next) => {
        if (accept(next)) setTraces((all) => ({ ...all, [name]: retain(all[name], next) }));
      });
      traceLoads.current.set(name, loading);
      void loading.finally(() => {
        if (traceLoads.current.get(name) === loading) traceLoads.current.delete(name);
      });
    }
    void loading.then(() => downloadTimings.current.set(key, performance.now() - startedAt));
    void loading.finally(() => reconcileBuilds.current());
    return loading;
  }, [pauseLoad]);

  const applyReady = useCallback(() => {
    if (CAPTURE_MODE || context.current.simulation || comparisonBusy.current) return;
    for (const [key, entry] of readyBuilds.current) {
      const id = entry.build_id;
      if (entry.status !== "ready" || id === undefined || !requested.current.has(key) ||
        pendingLoads.current.get(key) === "fresh" || loadedIds.current.get(key) === id ||
        attemptedIds.current.get(key) === id || obsoleteIds.current.get(key)?.has(id)) continue;
      attemptedIds.current.set(key, id);
      void load(entry.target, { build_id: id });
    }
  }, [load]);
  reconcileBuilds.current = applyReady;

  const receiveBuilds = useCallback((snapshot: BuildsSnapshot) => {
    latestFingerprint.current = snapshot.fingerprint;
    setWatchError((previous) => previous === snapshot.error ? previous : snapshot.error);
    if (snapshot.project !== null && catalogFingerprint.current !== snapshot.fingerprint) {
      catalogFingerprint.current = snapshot.fingerprint;
      const catalog = snapshot.project;
      const previous = context.current.project;
      if (previous !== null) {
        const updated = { ...previous, ...catalog };
        if (JSON.stringify([previous.parts, previous.folders, previous.assemblies, previous.variants, previous.default_variant]) !==
          JSON.stringify([updated.parts, updated.folders, updated.assemblies, updated.variants, updated.default_variant])) {
          setProject(updated);
          setTarget((current) => targetIn(updated, current));
        }
      }
    }
    readyBuilds.current.clear();
    let current: BackgroundBuild | null = null;
    for (const entry of snapshot.targets) {
      const key = keyOf(entry.target);
      readyBuilds.current.set(key, entry);
      if (context.current.target !== null && key === keyOf(context.current.target)) current = entry;
      // Queuing a later revision invalidates any older exact download still in flight.
      const pending = pendingLoads.current.get(key);
      if (entry.status !== "ready" && typeof pending === "object") {
        loadVersions.current.set(key, (loadVersions.current.get(key) ?? 0) + 1);
        pendingLoads.current.delete(key);
        attemptedIds.current.delete(key);
        pauseLoad(entry.target);
      }
    }
    setBackground((previous) => JSON.stringify(previous) === JSON.stringify(current) ? previous : current);
    applyReady();
  }, [applyReady, pauseLoad]);

  const targetKey = target === null ? null : keyOf(target);
  const watching = project !== null && !project.workspace_only && !CAPTURE_MODE;
  useEffect(() => {
    if (!watching) return;
    setConnection("connecting");
    setBackground(null);
    return listenBuilds(context.current.target, receiveBuilds, setConnection, setWatchError);
  }, [watching, targetKey, receiveBuilds]);
  useEffect(() => {
    applyReady();
  }, [simulation.enabled, applyReady]);
  const view = useRef<CadViewHandle>(null);
  const navigate = (next: Target) => {
    if (target?.kind === "assembly" && next.kind === "part") {
      const captured = view.current?.capture() ?? null;
      setAssemblyReturn({
        target,
        view: captured !== null && keyOf(captured.target) === keyOf(target) ? captured : null,
      });
    }
    setTarget(next);
  };
  const selectVariant = (variant: string) => {
    if (project === null || variant === activeVariant) return;
    const identity = target?.kind === "file" ? target.path : target?.name;
    const local = identity === undefined ? null
      : activeVariant === null ? identity : identity === activeVariant ? "" : identity.slice(activeVariant.length + 1);
    const qualified = local === null ? null : local === "" ? variant : `${variant}/${local}`;
    const candidate: Target | null = qualified === null || target === null ? null
      : target.kind === "file" ? { kind: "file", path: qualified } : { kind: target.kind, name: qualified };
    disableSimulation();
    setSavedView(null);
    setAssemblyReturn(null);
    setTarget(targetIn(project, candidate, variant));
  };
  const returnToAssembly = () => {
    if (assemblyReturn === null) return;
    setSavedView(assemblyReturn.view);
    setTarget(assemblyReturn.target);
  };
  const clearSelection = useCallback(() => {
    setSelection([]);
    setSelected(null);
    setFocusPart(null);
    setInstance(null);
    setLitPart(null);
  }, []);

  useEffect(() => {
    if (target?.kind !== "part" || project !== null && assemblyReturn !== null &&
      variantIn(project, assemblyReturn.target) !== activeVariant) setAssemblyReturn(null);
    setHiddenInstances(new Set());
    setHiddenParts(new Set());
    setSoloPart(null);
    setSoloInstance(null);
    setVisibilityTarget(target === null ? null : keyOf(target));
    if (target === null || (target.kind === "assembly" && target.name === "")) return;
    window.history.replaceState(null, "", hashOf(target));
    setSelected(null);
    setSelection([]);
    setCollapsedBy({});
    setFocusPart(null);
    setHover(null);
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
    if (CAPTURE_MODE) return;
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
  const selectedParts = useMemo(
    () => assemblyData !== null && selection.length > 0
      ? assemblySelectionParts(assemblyData, selection, target?.kind === "file") : [],
    [target, assemblyData, selection],
  );
  useEffect(() => {
    if (target?.kind !== "assembly") return;
    if (CAPTURE_MODE) return;
    for (const name of selectedParts) if (!requested.current.has(name)) void load({ kind: "part", name });
  }, [target, selectedParts, load]);
  const index = useMemo(() => trace === null ? null : treeIndex(trace), [trace]);
  const collapsed = (activePart !== null && collapsedBy[activePart]) || NO_NODES;
  const related = useMemo(
    () => (trace === null || index === null ? new Set<string>() : relatedTo(trace, index, selected)),
    [trace, index, selected],
  );
  const effectiveHidden = useMemo(() => {
    if (assemblyData === null || (soloPart === null && soloInstance === null && hiddenParts.size === 0)) return hiddenInstances;
    const root = assemblyData.scene.data.shapes;
    const leaves = shapeLeaves(root);
    const partLeaves = soloPart === null ? null : new Set(leaves.filter((leaf) =>
      (assemblyData.scene.part_of?.[leaf.id] ?? (target?.kind === "file" ? leaf.name : null)) === soloPart).map((leaf) => leaf.id));
    const node = soloInstance === null ? undefined : findShape(root, soloInstance);
    const instanceLeaves = node === undefined ? null : new Set(shapeLeaves(node).map((leaf) => leaf.id));
    return new Set(leaves.filter((leaf) => {
      // If a rebuild removed a soloed row, use that layer's eyes instead.
      const partVisible = partLeaves !== null && partLeaves.size > 0 ? partLeaves.has(leaf.id) : !hiddenParts.has(leaf.id);
      const instanceVisible = instanceLeaves !== null ? instanceLeaves.has(leaf.id) : !hiddenInstances.has(leaf.id);
      return !partVisible || !instanceVisible;
    }).map((leaf) => leaf.id));
  }, [soloPart, soloInstance, assemblyData, target, hiddenParts, hiddenInstances]);
  const toggleSoloPart = useCallback((part: string) => {
    setSoloPart((current) => current === part ? null : part);
  }, []);
  const toggleSoloInstance = useCallback((id: string) => {
    setSoloInstance((current) => current === id ? null : id);
  }, []);

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
      return {
        states,
        lit: [...new Set([...focuses.flatMap((own) => own.lit), ...entities])],
        ghost: focuses.some((own) => own.ghost),
        view: focuses.at(-1)?.view ?? null,
      };
    }
    if (assemblyData === null) return null;
    const root = assemblyData.scene.data.shapes;
    const base = assemblyFocus(root, effectiveHidden, null);
    const lit = selection.flatMap((item) => {
      if (item.kind === "entity") return [item.path];
      if (item.kind === "instance") {
        const node = findShape(root, item.path);
        return node === undefined ? [] : shapeLeaves(node).map((leaf) => leaf.id);
      }
      const inFile = target?.kind === "file" ? partInFile(assemblyData, traces, item.part) : null;
      return inFile === null ? [] : fileFocus(root, effectiveHidden, { ...inFile, selected: item.node }).lit;
    });
    return { ...base, lit: [...new Set(lit)] };
  }, [target, trace, index, selection, assemblyData, traces, effectiveHidden]);

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
    setHiddenParts(new Set());
    setSoloPart(null);
    setSoloInstance(null);
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
      const inFile = part === undefined ? null : partInFile(assemblyData, traces, part);
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
  const setPartVisibility = useCallback((leaves: string[], visible: boolean) => {
    setHiddenParts((current) => {
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
  const leafOf = (part: string) => (assemblyData === null ? null
    : leafOfPart(assemblyData.scene.data.shapes, part, assemblyData.scene.part_of));
  // Use only paths in this target's loaded scene, after its visibility reset has committed.
  const exportReady = target?.kind === "part" || (
    target !== null && assemblyData !== null && visibilityTarget === keyOf(target)
  );
  const exportShown = useMemo(() => {
    if (target === null || target.kind === "part" || assemblyData === null || !exportReady) return null;
    const root = assemblyData.scene.data.shapes;
    const hidden: string[] = [];
    const hiddenLeaves = new Set<string>();
    const collect = (node: ShapeNode) => {
      if (effectiveHidden.has(node.id)) {
        hidden.push(node.id);
        for (const leaf of shapeLeaves(node)) hiddenLeaves.add(leaf.id);
      } else {
        node.parts?.forEach(collect);
      }
    };
    collect(root);
    const leaves = shapeLeaves(root);
    return {
      hidden,
      visible: leaves.length - hiddenLeaves.size,
      of: leaves.length,
      unit: target.kind === "file" ? "parts" as const : "instances" as const,
    };
  }, [target, assemblyData, effectiveHidden, exportReady]);

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
  const live = useRef({
    target, index, assemblyData, selectIn, onPick, project, traces, selection, simulation, state,
    hiddenParts, hiddenInstances, soloPart, soloInstance, effectiveHidden, background, connection, watchError,
  });
  live.current = {
    target, index, assemblyData, selectIn, onPick, project, traces, selection, simulation, state,
    hiddenParts, hiddenInstances, soloPart, soloInstance, effectiveHidden, background, connection, watchError,
  };
  const inspectCurrent = useCallback((): Record<string, unknown> => {
    const current = live.current;
    const renderer = view.current?.inspect();
    return {
      ...renderer,
      phase: renderer?.phase ?? "mounting",
      ready: renderer?.ready === true && current.state?.status === "ok" && !current.simulation.enabled && !comparisonBusy.current,
      target: current.target, build_id: current.state?.status === "ok" ? current.state.data.build_id : null,
      status: current.state?.status ?? "none", selection: current.selection, hidden: [...current.effectiveHidden],
      visibility_layers: {
        ...renderer?.visibility_layers,
        parts: { hidden: [...current.hiddenParts], solo: current.soloPart },
        instances: { hidden: [...current.hiddenInstances], solo: current.soloInstance },
        hidden_parts: [...current.hiddenParts], hidden_instances: [...current.hiddenInstances],
        solo_part: current.soloPart, solo_instance: current.soloInstance, effective_hidden: [...current.effectiveHidden],
      },
      background: current.background, connection: current.connection, watch_error: current.watchError,
      download_ms: current.target === null ? null : downloadTimings.current.get(keyOf(current.target)) ?? null,
    };
  }, []);

  const compareBaseline = async (baseline: BaselineEntry) => {
    const handle = view.current;
    const current = live.current;
    if (comparisonBusy.current || handle === null || current.target === null || current.state?.status !== "ok") {
      throw new Error("Wait for the current build to finish rendering");
    }
    const target = current.target;
    const buildId = current.state.data.build_id;
    const original = current.state.data.scene.data;
    const saved = handle.capture();
    if (saved === null) throw new Error("No rendered camera is available");
    const guard = () => {
      if (live.current.target === null || keyOf(live.current.target) !== keyOf(target) ||
        live.current.state?.status !== "ok" || live.current.state.data.build_id !== buildId ||
        view.current?.identity !== handle.identity) {
        throw new Error("Target or build changed during comparison; images discarded");
      }
    };
    comparisonBusy.current = true;
    setComparisonActive(true);
    try {
      const pinned = await fetchBaselineScene(baseline.id, target);
      guard();
      await handle.rendered(original);
      const currentImages = await handle.screenshot(["current"]);
      guard();
      setSceneOverride(pinned.data.scene.data);
      await afterCommit();
      await handle.rendered(pinned.data.scene.data);
      handle.restore(saved);
      await afterCommit();
      const baselineImages = await handle.screenshot(["current"]);
      guard();
      if (!currentImages[0] || !baselineImages[0]) throw new Error("Comparison capture returned no image");
      setComparisonReview({
        baseline: { id: baseline.id, name: baseline.name ?? undefined, image: baselineImages[0].data, build_id: pinned.data.build_id },
        current: { image: currentImages[0].data, build_id: buildId },
      });
    } finally {
      setSceneOverride(null);
      await afterCommit();
      if (view.current?.identity === handle.identity && live.current.target !== null && keyOf(live.current.target) === keyOf(target) &&
        live.current.state?.status === "ok" && live.current.state.data.build_id === buildId) {
        await handle.rendered(original);
        handle.restore(saved);
        await afterCommit();
      }
      comparisonBusy.current = false;
      setComparisonActive(false);
      reconcileBuilds.current();
    }
  };

  const executeHub = useCallback(
    async (request: HubRequest): Promise<unknown> => {
      if (request.method === "inspect") return inspectCurrent();
      if (live.current.simulation.enabled) throw new Error("Live simulation is enabled. Return to CAD view before using CAD harness actions, exact measurements or snapshots.");
      const viewer = () => {
        if (view.current === null) throw new Error("Nothing is rendered");
        return view.current;
      };
      const params = request.params as Record<string, unknown>;
      const identity = (): BuildIdentity => {
        const current = live.current;
        if (current.target === null || current.state?.status !== "ok" || !current.state.data.build_id) throw new Error("No exact target/build is rendered");
        return { target: current.target, build_id: current.state.data.build_id,
          source_revision: current.state.data.source_revision, source_fingerprint: current.state.data.source_fingerprint };
      };
      const guard = (expected: BuildIdentity) => {
        const current = identity();
        if (keyOf(current.target) !== keyOf(expected.target) || current.build_id !== expected.build_id) {
          throw new Error(`Renderer navigation/build changed: expected ${JSON.stringify(expected)}, showing ${JSON.stringify(current)}; request discarded`);
        }
      };
      if (request.method !== "open" && request.method !== "capture") {
        if (params.expected === undefined) throw new Error("Renderer transaction requires expected target and build_id");
        guard(params.expected as BuildIdentity);
      }
      const restoreState = async (saved: SavedViewState) => {
        setHiddenInstances(new Set(saved.scene.hidden));
        setHiddenParts(new Set());
        setSoloPart(null);
        setSoloInstance(null);
        setSelection(saved.scene.selection);
        setSelected(saved.scene.selected);
        setFocusPart(saved.scene.focusPart);
        setInstance(saved.scene.instance);
        setLitPart(saved.scene.litPart);
        viewer().restore(saved);
        await afterCommit();
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

      const arrange = async (arrangement?: CaptureArrangement | null) => {
        if (arrangement == null) return;
        if (arrangement.selection !== undefined) applySelect({ items: arrangement.selection });
        if (arrangement.hidden !== undefined) {
          const root = live.current.assemblyData?.scene.data.shapes;
          if (root === undefined && arrangement.hidden.length > 0) throw new Error("Hidden instances require a composite target");
          const hidden = arrangement.hidden.flatMap((path) => {
            const node = root === undefined ? undefined : findShape(root, path);
            if (node === undefined) throw new Error(`Unknown hidden instance: ${path}`);
            return shapeLeaves(node).map((leaf) => leaf.id);
          });
          setHiddenInstances(new Set(hidden));
          setHiddenParts(new Set());
          setSoloPart(null);
          setSoloInstance(null);
        }
        await afterCommit();
        viewer().view(arrangement);
        if (arrangement.section !== undefined) viewer().section(arrangement.section);
        if (arrangement.explode !== undefined) viewer().explode(arrangement.explode);
        await afterCommit();
      };
      switch (request.method) {
        case "open":
        case "capture": {
          const asked = request.params as HubCalls["capture"]["params"];
          const startedAt = performance.now();
          if (!["part", "assembly", "file"].includes(asked.target.kind) || !asked.build_id) throw new Error("Open/capture requires an explicit target and exact build_id");
          const next = asked.target;
          const loading = load(next, { build_id: asked.build_id, baseline: asked.baseline });
          setTarget(next);
          const loaded = await loading;
          await afterCommit();
          if (loaded.status === "error") throw new Error(loaded.error);
          const downloadedAt = performance.now();
          const expected = { target: next, build_id: asked.build_id };
          if (loaded.build_id !== asked.build_id) throw new Error("Server substituted a different build; capture discarded");
          guard(expected);
          await viewer().rendered(loaded.scene);
          const renderedAt = performance.now();
          guard(expected);
          if (asked.select !== undefined && asked.select !== null) {
            applySelect({ items: [next.kind === "part" ? { kind: "feature", part: next.name, node: asked.select } : { kind: "instance", path: asked.select }] });
          }
          await arrange(asked.arrangement);
          await afterCommit();
          guard(expected);
          if (request.method === "open") return { status: "ok", ...expected };
          const images = asked.views.length === 0 ? [] : await viewer().screenshot(asked.views);
          guard(expected);
          return { ...identity(), images, renderer: inspectCurrent(), timings: {
            download_ms: downloadedAt - startedAt, render_ms: renderedAt - downloadedAt,
            capture_ms: performance.now() - renderedAt,
          } };
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
          guard(params.expected as BuildIdentity);
          return identity();
        case "section":
          viewer().section(request.params as HubCalls["section"]["params"]);
          await afterCommit();
          guard(params.expected as BuildIdentity);
          return identity();
        case "explode":
          viewer().explode((request.params as HubCalls["explode"]["params"]).amount);
          await afterCommit();
          guard(params.expected as BuildIdentity);
          return identity();
        case "saved_views": {
          const project = live.current.project;
          if (project === null) throw new Error("No project is loaded");
          const edited = editSavedViews(JSON.stringify([project.root, project.target]), request.params as HubCalls["saved_views"]["params"], () => viewer().capture());
          if (edited.restored !== undefined) {
            const saved = edited.restored;
            if (keyOf(saved.target) !== keyOf(identity().target)) {
              const loaded = await load(saved.target);
              setTarget(saved.target);
              await afterCommit();
              if (loaded.status === "error") throw new Error(loaded.error);
              guard({ target: saved.target, build_id: loaded.build_id });
              await viewer().rendered(loaded.scene);
            }
            applySelect({ items: saved.scene.selection });
            await restoreState(saved);
          }
          return { ...edited, ...identity() };
        }
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
          const asked = request.params as HubCalls["screenshot"]["params"];
          const expected = params.expected as BuildIdentity;
          const before = viewer().capture();
          try {
            await arrange(asked.arrangement);
            guard(expected);
            const images = await viewer().screenshot(asked.views, asked.width);
            guard(expected);
            return { ...identity(), images };
          } finally {
            if (before !== null && asked.arrangement != null && live.current.target !== null &&
                keyOf(live.current.target) === keyOf(expected.target) &&
                live.current.state?.status === "ok" && live.current.state.data.build_id === expected.build_id) await restoreState(before);
          }
        }
        default:
          throw new Error(`Unknown method: ${request.method}`);
      }
    },
    [load, afterCommit, setVisibility, clearSelection, inspectCurrent],
  );
  const transactionQueue = useRef<Promise<unknown>>(Promise.resolve());
  const executor = useRef(executeHub);
  executor.current = executeHub;
  const handled = useRef(new Map<string, { fingerprint: string; result: Promise<unknown>; settled: boolean }>());
  const handleHub = useCallback((request: HubRequest): Promise<unknown> => {
    const fingerprint = JSON.stringify([request.method, request.params]);
    const previous = handled.current.get(request.id);
    if (previous !== undefined) {
      if (previous.fingerprint !== fingerprint) return Promise.reject(new Error("Request id was reused for a different transaction"));
      return previous.result;
    }
    if (handled.current.size >= 256) {
      const removable = [...handled.current].find(([, value]) => value.settled);
      if (removable === undefined) return Promise.reject(new Error("Renderer transaction queue is full"));
      handled.current.delete(removable[0]);
    }
    const result = transactionQueue.current.then(() => executor.current(request)).then((value) => {
      // executeHub validated this transaction's identity before executing it.
      const params = request.params as { expected?: BuildIdentity };
      const expected = params.expected;
      if (expected !== undefined && request.method !== "saved_views") {
        const current = live.current;
        if (current.target === null || keyOf(current.target) !== keyOf(expected.target) ||
            current.state?.status !== "ok" || current.state.data.build_id !== expected.build_id) {
          throw new Error("Renderer navigation/build changed during transaction; result discarded");
        }
      }
      return value !== null && typeof value === "object" && !Array.isArray(value) ? { ...value, request_id: request.id } : value;
    });
    transactionQueue.current = result.catch(() => undefined);
    const record = { fingerprint, result, settled: false };
    handled.current.set(request.id, record);
    void result.finally(() => { record.settled = true; }).catch(() => undefined);
    return result;
  }, []);

  useEffect(() => {
    window.cadisteRenderer = { inspect: inspectCurrent };
    if (CAPTURE_MODE || HARNESS_MODE) {
      window.cadisteCapture = { protocol: "cadiste-renderer-v2", call: handleHub, inspect: inspectCurrent };
    }
    return () => { delete window.cadisteCapture; delete window.cadisteRenderer; };
  }, [handleHub, inspectCurrent]);

  const tabState: TabState = {
    target,
    build_id: state?.status === "ok" ? state.data.build_id : null,
    status: simulation.enabled ? "error" : state?.status ?? "none",
    error: simulation.enabled ? "Live simulation is enabled; measured poses are not static CAD geometry. Return to CAD view for harness actions." : state?.status === "error" ? state.error : null,
    shapes: simulation.enabled ? null : shapes,
    selection: simulation.enabled ? [] : selection,
    partShapes: Object.fromEntries(Object.entries(traces).flatMap(([part, load]) =>
      load.status === "ok" && load.shapes !== null ? [[part, load.shapes]] : [])),
    hidden: [...effectiveHidden],
    visibility_layers: {
      parts: { hidden: [...hiddenParts], solo: soloPart },
      instances: { hidden: [...hiddenInstances], solo: soloInstance },
      effective_hidden: [...effectiveHidden],
    },
  };
  const hub = useHub(tabState, handleHub, !CAPTURE_MODE);

  // ---- standalone snapshots: explicit selection first; pointed context only when it is empty ----

  const [notice, setNotice] = useState<{ text: string; failed: boolean } | null>(null);
  const [snapshotCopy, setSnapshotCopy] = useState<SnapshotCopyState | null>(null);
  const snapshotAttempt = useRef<{ cancel: () => void } | null>(null);
  const closeSnapshotCopy = () => {
    snapshotAttempt.current?.cancel();
    snapshotAttempt.current = null;
    setSnapshotCopy(null);
  };
  useEffect(() => () => { snapshotAttempt.current?.cancel(); }, []);
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

  const copyReference = (asked: TabState) => {
    // A repeated C/click must not generate a second snapshot or overwrite retained fallback text.
    if (snapshotAttempt.current !== null) return;
    const frozen = structuredClone(asked);
    const tab = hub.tab;
    const composite = assemblyData;
    const capturedTraces = traces;
    const label = frozen.selection.length === 0 ? "entire target" : `${frozen.selection.length} selected item(s)`;
    let cancelled = false;
    const attempt = { cancel: () => { cancelled = true; } };
    snapshotAttempt.current = attempt;
    setNotice(null);
    setSnapshotCopy({ phase: "generating" });

    // This promise is shared by the clipboard item and the fallback UI: exactly one server request.
    const generated = (async () => {
      if (live.current.simulation.enabled) throw new Error("Return to CAD view before copying a CAD selection snapshot. Measured simulation poses have no static CAD provenance.");
      if ((frozen.target?.kind === "assembly" || frozen.target?.kind === "file") && composite !== null) {
        const parts = assemblySelectionParts(composite, frozen.selection, frozen.target.kind === "file");
        const missing = parts.filter((part) => frozen.partShapes[part] === undefined);
        await Promise.all(missing.flatMap((part) => {
          const loading = traceLoads.current.get(part);
          if (loading !== undefined) return [loading];
          return capturedTraces[part] === undefined ? [load({ kind: "part", name: part })] : [];
        }));
        if (missing.length > 0) await afterCommit();
        if (cancelled) throw new Error("Snapshot copy cancelled");
        for (const part of missing) {
          const loaded = live.current.traces[part];
          if (loaded?.status !== "ok" || loaded.shapes === null) {
            const reason = loaded?.status === "error" ? loaded.error : "trace build is unavailable";
            throw new Error(`Feature context for ${part} could not be loaded: ${reason}`);
          }
          if (composite.source_fingerprint !== undefined && loaded.data.source_fingerprint !== undefined &&
              composite.source_fingerprint !== loaded.data.source_fingerprint) {
            throw new Error(`Feature context for ${part} changed since this selection was captured. Copy again from the updated view.`);
          }
          frozen.partShapes[part] = loaded.shapes;
        }
      }
      if (cancelled) throw new Error("Snapshot copy cancelled");
      if (live.current.simulation.enabled) throw new Error("Live simulation started before the CAD snapshot finished. Return to CAD view and copy again.");
      return createReference(tab, frozen);
    })();

    let written: Promise<boolean> = Promise.resolve(false);
    if (window.isSecureContext && typeof ClipboardItem !== "undefined" && navigator.clipboard?.write !== undefined) {
      const blob = generated.then(({ text }) => {
        if (cancelled) throw new Error("Snapshot copy cancelled");
        return new Blob([text], { type: "text/plain" });
      });
      // Some implementations reject construction/write before consuming the promised data.
      void blob.catch(() => undefined);
      try {
        // Do not await generation here: this call must occur in the original trusted C/click gesture.
        written = navigator.clipboard.write([new ClipboardItem({ "text/plain": blob })]).then(() => true, () => false);
      } catch {
        // Unsupported promise-backed items and synchronous denials use the same retained-text UI.
      }
    }
    void (async () => {
      try {
        const { text } = await generated;
        if (cancelled || snapshotAttempt.current !== attempt) return;
        setSnapshotCopy({ phase: "ready", text, copying: true });
        const copied = await written;
        if (cancelled || snapshotAttempt.current !== attempt) return;
        if (copied) {
          snapshotAttempt.current = null;
          setSnapshotCopy(null);
          setNotice({ text: `Copied snapshot: ${label}`, failed: false });
        } else {
          setSnapshotCopy({ phase: "ready", text, copying: false });
        }
      } catch (error) {
        if (cancelled || snapshotAttempt.current !== attempt) return;
        setSnapshotCopy({ phase: "failed", error: error instanceof Error ? error.message : String(error) });
      }
    })();
  };
  // The key handler reads this render's state through a ref: it is attached once.
  const copyPointed = useRef(() => {});
  copyPointed.current = () => {
    if (target !== null && state?.status !== "loading") void copyReference(referred());
  };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.repeat) return;
      if (event.key === "Escape" && snapshotAttempt.current !== null) {
        event.preventDefault();
        event.stopPropagation();
        snapshotAttempt.current.cancel();
        snapshotAttempt.current = null;
        setSnapshotCopy(null);
        return;
      }
      const typing = event.target instanceof HTMLElement && event.target.closest("input, textarea, select, [contenteditable], [role=dialog], .saved-views");
      if (typing) return;
      if (event.key === "Escape") {
        event.preventDefault();
        clearSelection();
        return;
      }
      if (event.key.toLowerCase() !== "c" || event.ctrlKey || event.metaKey || event.altKey) return;
      if (window.getSelection()?.isCollapsed === false) return;
      event.preventDefault();
      copyPointed.current();
    };
    // Captured: three-cad-viewer handles keys in its view.
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [clearSelection]);

  const rebuild = async () => {
    if (target === null) return;
    disableSimulation();
    setRefreshingProject(true);
    try {
      const loaded = await fetchProject();
      setWatchError(null);
      setProject(loaded);
      setTarget((current) => targetIn(loaded, current));
      const next = targetIn(loaded, target);
      if (next !== null && keyOf(next) === keyOf(target)) await load(target, "fresh");
    } catch (error: unknown) {
      setWatchError(failed(error).error);
    } finally {
      setRefreshingProject(false);
    }
  };

  if (projectError !== null) {
    return <div className="fatal">Cannot load the project: {projectError}</div>;
  }
  if (project?.workspace_only) return <div className="workspace-launcher"><RuntimeInfo /><ProjectLauncher /></div>;

  const targetIdentity = target?.kind === "file" ? target.path : target?.name;
  const targetLabel = activeVariant !== null && targetIdentity?.startsWith(`${activeVariant}/`)
    ? targetIdentity.slice(activeVariant.length + 1) : targetIdentity;
  const title = target?.kind === "file" ? targetLabel?.replaceAll("/", " / ") : targetLabel;
  const sceneData = simulation.enabled && simulation.scene !== null ? simulation.scene.scene.data
    : target?.kind === "part" ? (trace?.scene.data ?? null) : (assemblyData?.scene.data ?? null);
  const buildFailure = background?.status === "error"
    ? { error: background.error ?? "Background build failed", log: background.log }
    : state?.status === "error" ? state : state?.status === "ok" ? state.failure : undefined;
  const stale = state?.status === "ok" && (background?.status === "queued" || background?.status === "building" ||
    background?.status === "error" || state.refreshing || state.failure !== undefined ||
    background?.status === "ready" && background.build_id !== state.data.build_id &&
      !obsoleteIds.current.get(targetKey ?? "")?.has(background.build_id ?? ""));
  const buildStatus = background?.status === "queued" ? "Queued" : background?.status === "building" ? "Building" :
    buildFailure !== undefined ? "Build failed" :
    state?.status === "loading" || state?.status === "ok" && state.refreshing ? "Loading build" :
    stale ? simulation.enabled ? "CAD update deferred until returning to CAD" : "Loading latest build" : "Up to date";

  const partView = target?.kind === "part";
  const assemblyView = target?.kind === "assembly";
  const assemblyBack = partView && assemblyReturn !== null && project?.assemblies.includes(assemblyReturn.target.name)
    ? assemblyReturn : null;
  // Both part lists follow actual selection ownership, not the last focus (which can be toggled off).
  const markedPart = selectedParts.at(-1) ?? null;
  // A part, a file's parts or an assembly share the sidebar with what they list: features, parts and instances.
  const featurePane = target !== null;
  const contentsTitle = simulation.enabled ? "Simulation scene" : partView ? "Part features"
    : assemblyView ? "Assembly contents" : "Folder contents";
  const contentsCount = simulation.enabled ? null : partView ? (trace === null ? null : `${trace.ops.length} ops`)
    : target?.kind === "file" ? fileParts.length : assemblyData === null ? null : countLeaves(assemblyData.scene.data.shapes);
  const showDetails = trace !== null && index !== null && (partView || selected !== null);

  // A hidden panel leaves only its show button: the column shrinks to it. An assembly's view has none: its
  // parts and instances are in the sidebar.
  const panelColumn = assemblyView ? "" : panelOpen ? ` ${panelWidth}px` : " auto";
  const layout = assemblyView ? "app assembly wide" : partView ? "app" : "app assembly";

  return (
    <div className={layout} style={{ gridTemplateColumns: `${sidebarWidth}px minmax(0, 1fr)${panelColumn}` }}>
      <header className="topbar">
        <span className="brand">cadiste</span>
        {activeVariant !== null && (
          <label className="variant-selector">
            Variant
            <select value={activeVariant} onChange={(event) => selectVariant(event.target.value)}>
              {project?.variants?.map(({ id, label }) => <option key={id} value={id}>{label}</option>)}
            </select>
          </label>
        )}
        {project && <span className="project">{project.root}</span>}
        {!CAPTURE_MODE && project !== null && <ProjectLauncher currentRoot={project.root} />}
        <RuntimeInfo />
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
        <LinkedSessions sessions={hub.sessions.filter((session) => session.alive !== false)} tab={hub.tab} onUnlink={hub.unlink} />
        <button
          className="icon-button"
          aria-label="Copy standalone selection snapshot"
          data-action="copy-snapshot"
          disabled={simulation.enabled || target === null || state?.status === "loading"}
          aria-disabled={snapshotCopy !== null || undefined}
          onClick={() => void copyReference(tabState)}
          title="Copy a standalone snapshot of all selected items (no MCP lookup needed). C copies the selection; when empty, C uses pointed context. Ctrl/Cmd-click toggles; Shift-click adds; Escape clears."
        >
          <Icon name="links" />
        </button>
        <WorkflowTools target={target} buildId={state?.status === "ok" ? state.data.build_id : null}
          checkSets={Object.entries(project?.check_sets ?? {}).filter(([name, specification]) => {
            if (target === null) return false;
            const prefix = activeVariant === null ? "" : `${activeVariant}/`;
            if (prefix && !name.startsWith(prefix)) return false;
            const registered = specification.target;
            return registered.kind === target.kind && (registered.kind === "file"
              ? target.kind === "file" && `${prefix}${registered.path}` === target.path
              : target.kind !== "file" && `${prefix}${registered.name}` === target.name);
          }).map(([name]) => name)}
          selection={selection} disabled={simulation.enabled || comparisonActive}
          onCompare={compareBaseline} onResult={setWorkflowReport} />
        <ExportMenu target={target} title={title} shown={exportShown} ready={exportReady && !comparisonActive}
          manufacturing={activeVariant === null ? project?.manufacturing : project?.manufacturing_by_variant?.[activeVariant]}
          simulation={target?.kind === "assembly" && (project?.simulations.includes(target.name) ?? false)} />
        <button
          className="primary"
          disabled={target === null || refreshingProject || comparisonActive}
          onClick={() => void rebuild()}
          title="Rebuild only the current target, even if its cached build is up to date"
        >
          {refreshingProject ? "Building…" : "Rebuild"}
        </button>
      </header>

      <aside className="sidebar">
        {/* Project navigation above, the current target's contents below. */}
        <div
          className={featurePane ? "parts-pane" : "parts-pane full"}
          style={featurePane ? { gridTemplateRows: `minmax(0, ${partsHeight}px) auto auto` } : undefined}
        >
          <PartList
            parts={catalog.parts}
            folders={project?.folders ?? {}}
            assemblies={catalog.assemblies}
            variant={activeVariant}
            target={target}
            traces={traces}
            assemblyLoads={assemblies}
            files={files}
            marked={selectedParts}
            onSelect={navigate}
            divider={featurePane ? (
              <Splitter
                axis="y"
                value={partsHeight}
                min={90}
                max={Math.max(120, window.innerHeight - 260)}
                onChange={setPartsHeight}
              />
            ) : undefined}
          />
        </div>
        {featurePane && (
          <>
            <section className="sidebar-tree" aria-label={contentsTitle}>
              <header className="sidebar-section-header">
                <h2>{contentsTitle}</h2>
                {contentsCount !== null && <span className="sidebar-section-count">{contentsCount}</span>}
              </header>
              <div className="sidebar-section-context" title={title}>
                <Icon name={partView ? "part" : assemblyView ? "assembly" : "folder"} />
                <span>{title}</span>
              </div>
              {simulation.enabled && (
                <section className="simulation-scene-tree" aria-label="Simulation adapter scene">
                  <p className="muted">Measured TF and joint states move adapter nodes. CAD provenance and entity picking are disabled.</p>
                  {simulation.scene === null ? <p>{simulation.error ?? "Loading adapter geometry…"}</p>
                    : <ul><SimulationSceneNode node={simulation.scene.scene.data.shapes} /></ul>}
                </section>
              )}
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
                  variant={activeVariant}
                  traces={traces}
                  selected={focusPart !== null && selected !== null ? { part: focusPart, node: selected } : null}
                  selection={selection}
                  related={related}
                  collapsed={collapsedBy}
                  solo={soloPart}
                  onSolo={toggleSoloPart}
                  visible={(part) => {
                    const leaf = leafOf(part);
                    return leaf === null || !hiddenParts.has(leaf);
                  }}
                  onSelect={selectIn}
                  onToggle={toggleIn}
                  onVisibility={(part, visible) => {
                    const leaf = leafOf(part);
                    if (leaf !== null) setPartVisibility([leaf], visible);
                  }}
                  onHover={(part) => setHover(part === null ? null : leafOf(part))}
                  onOpen={(name) => navigate({ kind: "part", name })}
                />
              )}
              {assemblyView && assemblyData && !simulation.enabled && (
                <AssemblyPane
                  name={target.name}
                  data={assemblyData}
                  parts={catalog.parts}
                  variant={activeVariant}
                  hiddenParts={hiddenParts}
                  hiddenInstances={hiddenInstances}
                  soloPart={soloPart}
                  soloInstance={soloInstance}
                  onSoloPart={toggleSoloPart}
                  onSoloInstance={toggleSoloInstance}
                  instance={instance}
                  marked={markedPart}
                  selectedParts={selectedParts}
                  selectedPaths={new Set(selection.flatMap((item) => item.kind === "instance" ? [item.path] : []))}
                  onVisibility={setVisibility}
                  onPartVisibility={setPartVisibility}
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
                  onOpen={(name) => navigate({ kind: "part", name })}
                />
              )}
            </section>
          </>
        )}
        <Splitter axis="x" value={sidebarWidth} min={220} max={640} onChange={setSidebarWidth} />
      </aside>

      <main className="view">
        {target !== null && (
          <ViewBar target={target} project={project} variant={activeVariant} navigation={assemblyBack && (
            <button className="view-back" onClick={returnToAssembly} title={`Return to ${activeVariant === null ? assemblyBack.target.name : assemblyBack.target.name.slice(activeVariant.length + 1)}`}>
              Back to {activeVariant === null ? assemblyBack.target.name : assemblyBack.target.name.slice(activeVariant.length + 1)}
            </button>
          )}>
            {simulation.enabled && <strong className="simulation-view-title">Live simulation · {simulation.status} · measured world poses</strong>}
            {partView && trace && (
              <span className="stats">
                {trace.ops.length} ops · {trace.result.attributed}/{trace.result.faces} faces traced · built in{" "}
                {trace.build_seconds.toFixed(2)} s{cached && " · from cache"}
              </span>
            )}
            {assemblyData && !simulation.enabled && (
              <span className="stats">
                {countLeaves(assemblyData.scene.data.shapes)} {target.kind === "file" ? "parts" : "instances"} · built
                in {assemblyData.build_seconds.toFixed(2)} s{cached && " · from cache"}
              </span>
            )}
          </ViewBar>
        )}
        {watching && (
          <div className={`build-status-strip${buildFailure !== undefined || watchError !== null ? " failure" : ""}`}>
            {target !== null && <span role="status">{buildStatus}{stale && " · showing previous geometry"}</span>}
            <span className="stats">Source updates: {connection === "connected" ? "connected" :
              connection === "connecting" ? "connecting…" : "disconnected · reconnecting…"}</span>
            {(buildFailure !== undefined || watchError !== null) && <details>
              <summary>Build error details</summary>
              {watchError !== null && <pre>{watchError}</pre>}
              {buildFailure !== undefined && <><p>{buildFailure.error}</p>{buildFailure.log && <pre>{buildFailure.log}</pre>}</>}
            </details>}
          </div>
        )}
        <div className="view-body">
          <CadView
            key={activeVariant ?? ""}
            ref={view}
            data={sceneOverride ?? sceneData}
            focus={simulation.enabled || sceneOverride !== null ? null : focus}
            hover={simulation.enabled ? null : hover}
            onPick={simulation.enabled ? undefined : onPick}
            describe={simulation.enabled ? undefined : describeHover}
            shapes={simulation.enabled ? null : shapes}
            selectionPaths={simulation.enabled ? [] : selection.flatMap((item) => item.kind === "entity" ? [item.path] : [])}
            onDropPick={(path) => setSelection((current) => current.filter((item) => item.kind !== "entity" || item.path !== path))}
            onClearPicks={() => setSelection((current) => current.filter((item) => item.kind !== "entity"))}
            onClearSelection={clearSelection}
            viewScope={project === null ? null : JSON.stringify([project.root, project.target])}
            viewTarget={target}
            sceneState={{ hidden: [...effectiveHidden], selection, selected, focusPart, instance, litPart }}
            simulation={simulation}
            onRestoreView={(saved) => {
              setSavedView(saved);
              setTarget((current) => current !== null && keyOf(current) === keyOf(saved.target) ? current : saved.target);
            }}
          />
          {!simulation.enabled && !CAPTURE_MODE && <details className="solid-check"><summary>Solid check</summary>
            <SolidCheck shapes={shapes} selection={selection} onReview={(review) => view.current?.reviewCheck(review)} />
          </details>}
          {workflowReport !== null && <details className="workflow-report" open>
            <summary>Verification report · {workflowReport.ok ? "Pass" : "Fail"}</summary>
            <button onClick={() => { view.current?.reviewCheck(null); setWorkflowReport(null); }}>Close report</button>
            {(workflowReport.results as { target: Target; build_id?: string; checks?: { ok: boolean; result?: ExactCheckReport; error?: string }[]; assertions?: unknown[]; error?: string }[] ?? []).map((result, i) => <section key={i}>
              <strong>{result.target.kind === "file" ? result.target.path : result.target.name}</strong>
              {result.error && <p role="alert">{result.error}</p>}
              {(result.checks ?? []).map((check, j) => <div key={j}>
                {check.error && <p role="alert">{check.error}</p>}
                {check.result && Array.isArray(check.result.pairs) && <CheckReview result={check.result}
                  onReview={(review) => {
                    if (target === null || JSON.stringify(result.target) !== JSON.stringify(target) ||
                      state?.status !== "ok" || result.build_id !== state.data.build_id) {
                      throw new Error("This check belongs to another target/build; run it again for the displayed geometry");
                    }
                    view.current?.reviewCheck(review);
                  }} />}
              </div>)}
              <details><summary>Exact assertions and evidence</summary><pre>{JSON.stringify(result, null, 2)}</pre></details>
            </section>)}
          </details>}
          {comparisonReview !== null && <BaselineReview {...comparisonReview} onClose={() => setComparisonReview(null)} />}
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
          {project !== null && target?.kind === "assembly" && project.simulations.includes(target.name) && (
            <SimulationControls key={JSON.stringify([project.root, project.target, target.name])} target={target}
              live={simulation} onEnableLive={enableSimulation} onDisableLive={disableSimulation} />
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
      <LinkRequests sessions={hub.sessions.filter((session) => session.alive !== false)} onLink={hub.link} onDecline={hub.decline} />
      {snapshotCopy !== null && <SnapshotCopy state={snapshotCopy} onClose={closeSnapshotCopy} onCopied={() => {
        closeSnapshotCopy();
        setNotice({ text: "Copied snapshot", failed: false });
      }} />}
      {notice && <div role={notice.failed ? "alert" : "status"} className={notice.failed ? "notice failed" : "notice"}>{notice.text}</div>}
    </div>
  );
}
