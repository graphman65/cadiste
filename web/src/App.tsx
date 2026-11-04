import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { type Built, type BuildMode, fetchAssembly, fetchFile, fetchProject, fetchTrace, TraceFailure } from "./api";
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
import type { AssemblyData, HubCalls, HubRequest, Project, SceneData, ShapeNode, TabState, TraceData } from "./types";

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

export function App() {
  const [project, setProject] = useState<Project | null>(null);
  const [projectError, setProjectError] = useState<string | null>(null);
  const [target, setTarget] = useState<Target | null>(targetFromHash);
  const [traces, setTraces] = useState<Record<string, Load<TraceData>>>({});
  const [assemblies, setAssemblies] = useState<Record<string, Load<AssemblyData>>>({});
  const [files, setFiles] = useState<Record<string, Load<AssemblyData>>>({});
  const [selected, setSelected] = useState<string | null>(null);
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
    setTraces((all) => ({ ...all, [name]: { status: "loading" } }));
    return settle(fetchTrace(name, mode), (load) => setTraces((all) => ({ ...all, [name]: load })));
  }, []);
  const view = useRef<CadViewHandle>(null);
  const [picks, setPicks] = useState<string[]>([]);

  useEffect(() => {
    if (target === null || (target.kind === "assembly" && target.name === "")) return;
    window.history.replaceState(null, "", hashOf(target));
    setSelected(null);
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
  const index = trace === null ? null : treeIndex(trace);
  const collapsed = (activePart !== null && collapsedBy[activePart]) || NO_NODES;
  const related = useMemo(
    () => (trace === null || index === null ? new Set<string>() : relatedTo(trace, index, selected)),
    [trace, index, selected],
  );
  const focus = useMemo(() => {
    if (target?.kind === "part") return trace !== null && index !== null ? sceneFocus(trace, index, selected) : null;
    if (assemblyData === null) return null;
    const root = assemblyData.scene.data.shapes;
    if (target?.kind === "file") {
      const inFile = focusPart === null ? null : partInFile(assemblyData.scene.data, traces, focusPart);
      return fileFocus(root, hiddenInstances, inFile && selected !== null ? { ...inFile, selected } : null);
    }
    const partInstances = Object.entries(assemblyData.scene.part_of ?? {}).filter(([, part]) => part === litPart);
    return assemblyFocus(root, hiddenInstances, instance, litPart === null ? [] : partInstances.map(([leaf]) => leaf));
  }, [target, trace, index, selected, assemblyData, focusPart, traces, hiddenInstances, instance, litPart]);

  /** Select a feature of a part (in a file's view, any of its parts), opening the folds above it. */
  const selectIn = useCallback(
    (part: string | null, id: string | null) => {
      setFocusPart(part);
      setSelected(id);
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
  const select = useCallback((id: string | null) => selectIn(activePart, id), [selectIn, activePart]);

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
    (path: string) => {
      if (target?.kind === "part") {
        if (trace !== null) select(nodeForPick(trace, path));
        return;
      }
      const inFile = pickInFile(path);
      if (inFile !== null) selectIn(inFile.part, nodeForPick(inFile.trace, inFile.path));
      else if (assemblyData !== null) {
        setInstance(leafOfPick(assemblyData.scene.data.shapes, path));
        setLitPart(null);
      }
    },
    [target, trace, assemblyData, select, selectIn, pickInFile],
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
  const live = useRef({ target, index, assemblyData, selectIn, project });
  live.current = { target, index, assemblyData, selectIn, project };

  const handleHub = useCallback(
    async (request: HubRequest): Promise<unknown> => {
      const viewer = () => {
        if (view.current === null) throw new Error("Nothing is rendered");
        return view.current;
      };
      /** Sets the selection a `select` call (or `open`) asks for, once all of it is known to exist. */
      const applySelect = ({ node, instance: chosen, picks: paths }: HubCalls["select"]["params"]) => {
        const { target: current, index: tree, assemblyData: composite, selectIn: choose } = live.current;
        if (node !== undefined && node !== null) {
          if (current?.kind !== "part") throw new Error("Only a part's view has tree nodes to select");
          if (!tree?.nodes.has(node)) throw new Error(`Unknown tree node: ${node}`);
        }
        if (chosen !== undefined && chosen !== null) {
          if (composite === null) throw new Error("Only the assembly's or a file's view has instances to select");
          if (findShape(composite.scene.data.shapes, chosen) === undefined) throw new Error(`Unknown instance: ${chosen}`);
        }
        if (paths !== undefined) {
          const unknown = viewer().setPicks(paths);
          if (unknown.length > 0) throw new Error(`Unknown viewer paths: ${unknown.join(", ")}`);
        }
        if (node !== undefined && current?.kind === "part") choose(current.name, node);
        if (chosen !== undefined) {
          setInstance(chosen);
          setLitPart(null);
        }
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
            applySelect(next.kind === "part" ? { node: chosen } : { instance: chosen });
          }
          await afterCommit();
          return { status: "ok" } satisfies HubCalls["open"]["result"];
        }
        case "select":
          applySelect(request.params as HubCalls["select"]["params"]);
          await afterCommit();
          // The view reports its picks from an effect of that commit: they reach this state one commit later.
          await afterCommit();
          return {};
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
    [load, afterCommit, setVisibility],
  );

  const tabState: TabState = {
    target,
    status: state?.status ?? "none",
    error: state?.status === "error" ? state.error : null,
    shapes,
    selected,
    picks,
    instance,
    hidden: [...hiddenInstances],
  };
  const hub = useHub(tabState, handleHub);

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
        {trace && (
          <>
            <button onClick={() => setCollapsed(new Set())}>Expand all</button>
            <button onClick={collapseAll}>Collapse all</button>
          </>
        )}
        <LinkedSessions sessions={hub.sessions} tab={hub.tab} onUnlink={hub.unlink} />
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
                  litPart={litPart}
                  marked={markedPart}
                  onVisibility={setVisibility}
                  onSelectInstance={(id) => {
                    setInstance(id);
                    setLitPart(null);
                  }}
                  onSelectPart={(part) => {
                    setLitPart(part);
                    setInstance(null);
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
            onPicks={setPicks}
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
        <Timeline trace={trace} index={index} selected={selected} onSelect={select} />
      ) : (
        partView && <footer className="timeline" />
      )}
      <LinkRequests sessions={hub.sessions} onLink={hub.link} onDecline={hub.decline} />
    </div>
  );
}
