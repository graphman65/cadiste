import { type ReactNode, type Ref, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from "react";
import {
  Display,
  type RenderOptions,
  type Shapes,
  Viewer,
  type ViewerOptions,
  type Vector3Tuple,
} from "three-cad-viewer";
import "three-cad-viewer/css";

import type { Target } from "../App";
import type { Focus } from "../model";
import { selectionMode, type SelectionMode } from "../selection";
import type { LiveSimulation } from "../simulation";
import type { HubCalls, SceneData, SimulationPose, SimulationScene } from "../types";
import { type SceneHover, Readout } from "./HoverCard";
import { MeasureMarks, MeasurePanel, type Picked, measurementOf, useMeasurement } from "./Measure";
import { SectionPanel, useSection } from "./Section";
import { SavedViews, type SavedViewState, type SceneViewState } from "./SavedViews";
import { type ViewDirection, type ViewSettings, ViewToolbar, useViewSettings } from "./ViewToolbar";

// OCP CAD Viewer's defaults, so models look the way they do there.
const RENDER_OPTIONS: RenderOptions = {
  ambientIntensity: 1,
  directIntensity: 1.1,
  metalness: 0.3,
  roughness: 0.65,
  edgeColor: 0x707070,
  defaultOpacity: 0.5,
  normalLen: 0,
};
const TREE_WIDTH = 240;
const SCREENSHOT_WIDTH = 1024;

/** The scene's operations driven by the app and by a harness session through the hub. */
export interface CadViewHandle {
  /** Resolves once this scene is rendered and focused: at once if it is the one shown. */
  rendered: (data: SceneData) => Promise<void>;
  /** Validate entity paths without changing the selection or measurement mode. */
  unknownPaths: (paths: string[]) => string[];
  /** Light these paths on top of the selection until the next call; returns the paths unknown. */
  highlight: (paths: string[]) => string[];
  view: (params: HubCalls["view"]["params"]) => void;
  /** PNGs of the scene (base64), along each direction (none: as it is), the camera put back after. */
  screenshot: (views: ViewDirection[], width?: number) => Promise<HubCalls["screenshot"]["result"]["images"]>;
  /** The viewer path of the face, edge, vertex or object under the pointer (null: none). */
  hovered: () => string | null;
  /** Restore viewer-owned state after the app has opened the saved target and restored its selection. */
  restore: (state: SavedViewState) => void;
}

function viewerOptions(settings: ViewSettings): ViewerOptions {
  return {
    up: "Z",
    control: "orbit",
    ortho: settings.ortho,
    axes: settings.axes,
    grid: [settings.grid, false, false],
    blackEdges: settings.edges,
  };
}

interface Props {
  /** What to render; a new object renders anew, null keeps what is shown. */
  data: SceneData | null;
  /** Applied after every render and whenever it changes; null leaves the scene as rendered. */
  focus: Focus | null;
  /** A viewer path to hover-highlight (a leaf or a whole group), as when hovering a tree row. */
  hover?: string | null;
  /** A click (not a drag) on something in the scene, with its viewer path. */
  onPick?: (path: string, mode: SelectionMode) => void;
  /** What a hovered component belongs to (the feature that made it, the instance), shown above its geometry. */
  describe?: (path: string) => ReactNode;
  /** The build's shapes file, to measure on (`Built.shapes`); null: this build cannot be measured. */
  shapes?: string | null;
  /** Canonical exact entity picks, shared by selection and the measurement tool. */
  selectionPaths: string[];
  onDropPick: (path: string) => void;
  onClearPicks: () => void;
  onClearSelection: () => void;
  /** Project identity, for browser-local saved views shared across targets. */
  viewScope: string | null;
  viewTarget: Target | null;
  sceneState: SceneViewState;
  onRestoreView: (state: SavedViewState) => void;
  simulation: LiveSimulation;
  ref?: Ref<CadViewHandle>;
}

/** The tree leaf a registry component belongs to: its solid, or itself without its topology suffix. */
function leafOf(info: { path: string; solidPath: string | null }): string {
  return info.solidPath ?? info.path.replace(/\/(faces|edges|vertices)\/[^/]+$/, "");
}

/** A PNG data URL as base64 PNG at most `width` px wide, its aspect kept. */
async function scaled(dataUrl: string, width: number): Promise<string> {
  const image = new Image();
  image.src = dataUrl;
  await image.decode();
  const scale = Math.min(1, width / image.naturalWidth);
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
  canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
  const context = canvas.getContext("2d");
  if (context === null) throw new Error("Cannot draw the screenshot");
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/png").slice("data:image/png;base64,".length);
}

interface Instance {
  display: Display;
  viewer: Viewer;
  /** Size the canvas to the div. */
  fit: () => void;
}

function displayOptions(width: number, height: number, settings: ViewSettings) {
  return {
    cadWidth: Math.max(width, 200),
    height: Math.max(height, 200),
    treeWidth: TREE_WIDTH,
    theme: "dark" as const,
    glass: true,
    tools: settings.tools,
    pinning: false,
    measureTools: true,
    selectTool: true,
    explodeTool: true,
    zebraTool: true,
    studioTool: true,
    zscaleTool: false,
    // Measurements answered in the browser, from the mesh: there is no measurement backend.
    externalMeasurementBackend: false,
  };
}

// Structural types keep the renderer's own THREE instance: no second bundled Three.js runtime.
interface LiveMatrix {
  clone: () => LiveMatrix;
  set: (...values: number[]) => LiveMatrix;
  invert: () => LiveMatrix;
  multiply: (matrix: LiveMatrix) => LiveMatrix;
  decompose: (position: unknown, quaternion: unknown, scale: unknown) => void;
}
interface LiveNode {
  matrix: LiveMatrix;
  position: unknown;
  quaternion: unknown;
  scale: unknown;
  updateMatrix: () => void;
  updateMatrixWorld: (force: boolean) => void;
}

/** Rigid transform; Matrix4.set takes row-major entries. Measured SI translations become CAD millimetres. */
function poseMatrix(pose: SimulationPose, translationScale: number, matrix: LiveMatrix): LiveMatrix {
  const [x, y, z, w] = pose.quaternion;
  const [px, py, pz] = pose.position;
  return matrix.set(
    1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), px * translationScale,
    2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), py * translationScale,
    2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), pz * translationScale,
    0, 0, 0, 1,
  );
}

/**
 * three-cad-viewer (OCP CAD Viewer's renderer) in a div it owns, with the app's own toolbar over it; one viewer,
 * Section-plane picking consumes a click first. Measurement mode keeps its additive picks in the same canonical
 * selection as tree/scene items, so copying never omits measurements or resets unrelated selected items.
 */
export function CadView({ data, focus, hover = null, onPick, describe, shapes = null, selectionPaths, onDropPick, onClearPicks, onClearSelection, viewScope, viewTarget, sceneState, onRestoreView, simulation, ref }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const instance = useRef<Instance | null>(null);
  const [rendered, setRendered] = useState(0);
  const lastView = useRef<string | null>(null);
  const [settings, changeSettings] = useViewSettings();
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  const [hovered, setHovered] = useState<SceneHover | null>(null);
  /** `hovered`'s path, for the handle: read when a key is pressed, not rendered. */
  const hoveredPath = useRef<string | null>(null);
  const card = useRef<HTMLDivElement>(null);
  /** The pointer, relative to the view. */
  const pointer = useRef({ x: 0, y: 0 });
  const [measuring, setMeasuring] = useState(false);
  /** Mirrors three-cad-viewer's explode mode, which its own toolbar can turn on too and a new render turns off. */
  const [exploded, setExploded] = useState(false);
  const [picked, setPicked] = useState<Picked[]>([]);
  const measureState = useMeasurement(measuring ? shapes : null, picked);
  const viewerOf = useCallback(() => instance.current?.viewer ?? null, []);
  /** Paths a harness session lit, on top of the selection. */
  const [extra, setExtra] = useState<string[]>([]);
  /** The scene last rendered, and who waits for a scene to be. */
  const shown = useRef<SceneData | null>(null);
  const waiting = useRef<{ data: SceneData; resolve: () => void }[]>([]);
  const section = useSection(viewerOf, rendered);
  const { picking: sectionPicking, pickFace: pickSectionFace } = section;
  const pendingView = useRef<SavedViewState | null>(null);
  const simulationMode = useRef(simulation.enabled);
  simulationMode.current = simulation.enabled;
  const liveNodes = useRef<{ scene: SimulationScene; nodes: { link: string; node: LiveNode; restToInitial: LiveMatrix }[]; fitted: boolean } | null>(null);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [appliedStamp, setAppliedStamp] = useState<number | null>(null);

  /** The hover card beside the pointer, flipped to its other side near the view's right and bottom edges. */
  const placeCard = () => {
    const element = card.current;
    const view = host.current;
    if (element === null || view === null) return;
    const gap = 14;
    const { x, y } = pointer.current;
    const { offsetWidth: width, offsetHeight: height } = element;
    const left = x + gap + width > view.clientWidth ? x - gap - width : x + gap;
    const top = y + gap + height > view.clientHeight ? y - gap - height : y + gap;
    element.style.transform = `translate(${Math.max(0, left)}px, ${Math.max(0, top)}px)`;
  };
  useLayoutEffect(placeCard, [hovered]);

  useEffect(() => {
    const element = host.current;
    if (element === null) return;
    const { width, height } = element.getBoundingClientRect();
    const options = displayOptions(width, height, settingsRef.current);
    const display = new Display(element, options);
    const viewer = new Viewer(display, options, null);
    const handleHover = viewer.pickingController.handleHover.bind(viewer.pickingController);
    viewer.pickingController.handleHover = () => {
      if (!simulationMode.current) handleHover();
    };
    // three-cad-viewer reads out what the pointer is over (or a copied point) on a status line under the canvas,
    // set as the hovered component changes; the hover card shows it instead.
    display.setStatusLine = (readout: string) => {
      if (simulationMode.current) return;
      const picked = viewer.lastObject;
      hoveredPath.current = readout === "" ? null : (picked?.backendId ?? null);
      setHovered(readout === "" ? null : { path: picked?.backendId ?? null, topo: picked?.topo ?? null, readout });
    };
    const stopExplodeSync = viewer.state.subscribe("animationMode", (change) => setExploded(change.new === "explode"));
    const onMove = (event: PointerEvent) => {
      const box = element.getBoundingClientRect();
      pointer.current = { x: event.clientX - box.left, y: event.clientY - box.top };
      // A drag orbits the camera, during which three-cad-viewer keeps its last readout: no card until it ends.
      element.classList.toggle("dragging", event.buttons !== 0);
      placeCard();
    };
    element.addEventListener("pointermove", onMove);
    const guardLiveKeys = (event: KeyboardEvent) => {
      if (simulationMode.current) {
        event.stopImmediatePropagation();
        event.preventDefault();
      }
    };
    element.addEventListener("keydown", guardLiveKeys, true);
    const fit = () => {
      if (!viewer.ready) return; // resizeCadView throws before the first render
      const box = element.getBoundingClientRect();
      // three-cad-viewer's toolbar, when shown, sits above the canvas: give the canvas what is left below it.
      const canvas = element.querySelector(".tcv_cad_view_glass, .tcv_cad_view");
      const chrome = canvas ? canvas.getBoundingClientRect().top - box.top : 0;
      viewer.resizeCadView(Math.max(box.width, 200), TREE_WIDTH, Math.max(box.height - chrome, 200), true);
    };
    instance.current = { display, viewer, fit };
    const observer = new ResizeObserver(fit);
    observer.observe(element);
    return () => {
      stopExplodeSync();
      element.removeEventListener("pointermove", onMove);
      element.removeEventListener("keydown", guardLiveKeys, true);
      observer.disconnect();
      viewer.dispose();
      display.dispose();
      instance.current = null;
    };
  }, []);

  useEffect(() => {
    const current = instance.current;
    if (current === null || data === null) return;
    liveNodes.current = null;
    if (current.viewer.ready) current.viewer.clear();
    // render() takes the instanced format too (it decodes it first), but its type only names the decoded tree.
    const shapes = data as unknown as Shapes;
    current.viewer.render(shapes, RENDER_OPTIONS, viewerOptions(settingsRef.current));
    current.fit();
    lastView.current = null;
    shown.current = data;
    setRendered((count) => count + 1);
  }, [data]);

  useEffect(() => {
    const viewer = instance.current?.viewer;
    if (viewer === undefined || !viewer.ready) return;
    viewer.setPickHandler(!simulation.enabled);
    viewer.setSelectionInput(false);
    if (simulation.enabled) {
      setMeasuring(false);
      section.open(false);
      viewer.setExplode(false);
      viewer.setLocalClipping(false);
      viewer.pickingController.clearHover();
      hoveredPath.current = null;
      setHovered(null);
      setExtra([]);
    }
  }, [simulation.enabled, rendered]);

  useEffect(() => {
    if (simulation.enabled && liveNodes.current?.scene === simulation.scene) return;
    liveNodes.current = null;
    setLiveError(null);
    setAppliedStamp(null);
    const viewer = instance.current?.viewer;
    const scene = simulation.scene;
    if (!simulation.enabled || scene === null || viewer === undefined || !viewer.ready || shown.current !== scene.scene.data) return;
    try {
      const nodes = Object.entries(scene.links).map(([link, mapping]) => {
        const group = viewer.rendered.nestedGroup.groups[mapping.path];
        if (group === undefined) throw new Error(`Adapter scene has no mapped node for ${link}: ${mapping.path}`);
        const node = group as unknown as LiveNode;
        node.updateMatrix();
        // Preserve all initial shape offsets. Never multiply a delta into last frame's pose.
        const restToInitial = poseMatrix(mapping.rest, 1, node.matrix.clone()).invert().multiply(node.matrix);
        return { link, node, restToInitial };
      });
      liveNodes.current = { scene, nodes, fitted: false };
    } catch (error: unknown) {
      setLiveError(error instanceof Error ? error.message : String(error));
    }
  }, [simulation.enabled, simulation.scene, rendered]);

  useEffect(() => {
    const viewer = instance.current?.viewer;
    const mapping = liveNodes.current;
    const feedback = simulation.feedback;
    if (!simulation.enabled || mapping === null || mapping.scene !== simulation.scene || viewer === undefined || feedback === null) return;
    // Before the first complete sample, do not present CAD rest geometry as a measured robot.
    if (mapping.nodes.some(({ link }) => feedback.links[link] === undefined)) return;
    for (const { link, node, restToInitial } of mapping.nodes) {
      poseMatrix(feedback.links[link], 1000, node.matrix).multiply(restToInitial);
      node.matrix.decompose(node.position, node.quaternion, node.scale);
      node.updateMatrixWorld(true);
    }
    // Public API recomputes world bounds, camera clip ranges, reset target and picker/hover caches.
    // It does not retessellate geometry or move the current camera.
    viewer.updateBounds();
    if (!mapping.fitted) {
      viewer.reset();
      mapping.fitted = true;
    }
    viewer.idPicker?.setDirty();
    viewer.update(true, false);
    setAppliedStamp(feedback.stamp);
  }, [simulation.enabled, simulation.scene, simulation.feedback, rendered]);

  useEffect(() => {
    const current = instance.current;
    if (current === null || !current.viewer.ready) return;
    const { viewer } = current;
    viewer.setOrtho(settings.ortho);
    viewer.setAxes(settings.axes);
    viewer.setGrids([settings.grid, false, false]);
    viewer.setBlackEdges(settings.edges);
    const tools = settings.tools && !simulation.enabled;
    if (viewer.getTools() !== tools) {
      viewer.showTools(tools);
      current.fit();
    }
  }, [settings, rendered, simulation.enabled]);

  useEffect(() => {
    const viewer = instance.current?.viewer;
    if (viewer === undefined || !viewer.ready || focus === null) return;
    viewer.setStates(focus.states);
    viewer.setTransparent(focus.ghost || settings.transparent);
    const group = viewer.rendered.nestedGroup;
    const highlight = group.highlight;
    highlight?.clear();
    if (highlight) {
      const lit = new Set(focus.lit);
      for (const info of group.registry.entries()) {
        if (lit.has(info.path) || (info.topo === "face" && lit.has(leafOf(info)))) highlight.setSelected(info.id, true);
      }
    }
    if (highlight && extra.length > 0) {
      // A component lights exactly; a leaf, all its faces.
      const lit = new Set(extra);
      for (const info of group.registry.entries()) {
        if (lit.has(info.path) || (info.topo === "face" && lit.has(leafOf(info)))) highlight.setSelected(info.id, true);
      }
    }
    // Turn to a sketch once when it is selected, not on every focus change after (the user may orbit away).
    if (focus.view !== null && focus.view !== lastView.current) viewer.presetCamera(focus.view);
    lastView.current = focus.view;
    viewer.update(true, false);
  }, [focus, settings.transparent, rendered, extra]);

  // Restore after focus has applied its visibility and optional sketch camera. Otherwise focus would undo it.
  useEffect(() => {
    const saved = pendingView.current;
    const viewer = instance.current?.viewer;
    if (saved === null || viewer === undefined || !viewer.ready) return;
    pendingView.current = null;
    const known = viewer.getStates();
    viewer.setStates(Object.fromEntries(Object.entries(saved.states).filter(([path]) => path in known)));
    // Perspective zoom is already encoded by position. Reapplying it dollies around the renderer's original
    // centre, not a panned orbit target. Orbit's public quaternion setter also recomputes a look-at rotation.
    viewer.setCameraLocationSettings(saved.camera.position, null, saved.camera.target, saved.settings.ortho ? saved.camera.zoom : null, false);
    viewer.rendered.camera.setQuaternion(saved.camera.quaternion);
    viewer.update(true, false);
  }, [focus, settings, rendered]);

  // After the focus effect: a scene waited for is focused too once this runs.
  useEffect(() => {
    const ready = waiting.current.filter((waiter) => waiter.data === shown.current);
    waiting.current = waiting.current.filter((waiter) => waiter.data !== shown.current);
    ready.forEach((waiter) => waiter.resolve());
  }, [rendered]);

  const selectedPathsKey = selectionPaths.join("\n");
  useEffect(() => {
    const viewer = instance.current?.viewer;
    if (viewer === undefined || !viewer.ready) return;
    const selected = new Set(selectedPathsKey === "" ? [] : selectedPathsKey.split("\n"));
    const next: Picked[] = [];
    for (const info of viewer.rendered.nestedGroup.registry.entries()) {
      if (info.topo !== "solid" && selected.has(info.path)) next.push({ path: info.path, id: info.id, topo: info.topo });
    }
    setPicked(next);
  }, [selectedPathsKey, rendered]);

  useImperativeHandle(
    ref,
    (): CadViewHandle => {
      const readyViewer = () => {
        if (simulationMode.current) throw new Error("Return to CAD view before using CAD harness actions or snapshots.");
        const viewer = instance.current?.viewer;
        return viewer !== undefined && viewer.ready ? viewer : null;
      };
      const registry = () => readyViewer()?.rendered.nestedGroup.registry.entries() ?? [];
      return {
        hovered: () => hoveredPath.current,
        restore: (saved) => {
          if (simulationMode.current) throw new Error("Return to CAD view before restoring a saved CAD view.");
          pendingView.current = saved;
          setExtra([]);
          changeSettings(saved.settings);
          section.restore(saved.section.active, saved.section.section);
          setMeasuring(saved.measuring);
          const viewer = readyViewer();
          if (viewer !== null) {
            viewer.setExplode(saved.explode !== null);
            if (saved.explode !== null) viewer.setRelativeTime(saved.explode);
          }
        },
        rendered: (scene) =>
          new Promise((resolve) => {
            if (shown.current === scene) resolve();
            else waiting.current.push({ data: scene, resolve });
          }),
        unknownPaths: (paths) => {
          const known = new Set<string>();
          for (const info of registry()) known.add(info.path).add(leafOf(info));
          return paths.filter((path) => !known.has(path));
        },
        highlight: (paths) => {
          const known = new Set<string>();
          for (const info of registry()) known.add(info.path).add(leafOf(info));
          const unknown = paths.filter((path) => !known.has(path));
          if (unknown.length === 0) setExtra(paths);
          return unknown;
        },
        view: ({ direction, fit, ortho, transparent }) => {
          const viewer = readyViewer();
          if ((direction !== undefined || fit) && viewer === null) throw new Error("Nothing is rendered");
          if (direction !== undefined) viewer?.presetCamera(direction);
          if (fit) viewer?.reset();
          const change = { ...(ortho === undefined ? {} : { ortho }), ...(transparent === undefined ? {} : { transparent }) };
          if (Object.keys(change).length > 0) changeSettings(change);
        },
        screenshot: async (views, width = SCREENSHOT_WIDTH) => {
          const viewer = readyViewer();
          if (viewer === null || shown.current === null) throw new Error("Nothing is rendered");
          const [x, y, z] = viewer.getCameraPosition();
          const position: Vector3Tuple = [x, y, z];
          const quaternion = viewer.getCameraQuaternion();
          const target = viewer.getCameraTarget();
          const zoom = viewer.getCameraZoom();
          const images: HubCalls["screenshot"]["result"]["images"] = [];
          try {
            for (const view of views.length > 0 ? views : [null]) {
              if (view !== null) viewer.presetCamera(view, null, false);
              const image = await viewer.getImage(`cadiste-${view ?? "current"}`);
              if (typeof image.dataUrl !== "string") throw new Error("The view could not be captured");
              images.push({ view: view ?? "current", data: await scaled(image.dataUrl, width) });
            }
          } finally {
            // presetCamera moves the target to the scene's centre: the target first, then where the camera was.
            viewer.setCameraTarget(target, false);
            viewer.setCameraPosition(position, false, false);
            viewer.setCameraQuaternion(quaternion, false);
            viewer.setCameraZoom(zoom, false);
          }
          return images;
        },
      };
    },
    [changeSettings, section.restore],
  );

  useEffect(() => {
    const viewer = instance.current?.viewer;
    if (viewer !== undefined && viewer.ready) viewer.handleTreeHover(hover);
  }, [hover, rendered]);

  useEffect(() => {
    const viewer = instance.current?.viewer;
    if (viewer === undefined) return;
    const canvas = viewer.renderer.domElement;
    let down: { x: number; y: number } | null = null;
    const onDown = (event: PointerEvent) => {
      down = { x: event.clientX, y: event.clientY };
    };
    const onUp = (event: PointerEvent) => {
      if (simulationMode.current) return;
      // A drag orbits the camera; only a click picks.
      if (down === null || Math.hypot(event.clientX - down.x, event.clientY - down.y) > 4) return;
      const box = canvas.getBoundingClientRect();
      const [x, y] = [event.clientX - box.left, event.clientY - box.top];
      if (sectionPicking) {
        const face = viewer.ready ? viewer.idPicker?.pickAt(x, y, { topoFilter: ["face"] }) : null;
        if (face) pickSectionFace(face.info.path);
        return;
      }
      const hit = viewer.ready ? viewer.idPicker?.pickAt(x, y) : null;
      if (!measuring) {
        if (hit) onPick?.(hit.info.path, selectionMode(event));
        else if (!event.ctrlKey && !event.metaKey && !event.shiftKey) onClearSelection();
      } else if (!hit) {
        if (!event.ctrlKey && !event.metaKey && !event.shiftKey) onClearPicks();
      } else if (hit.info.topo !== "solid") {
        onPick?.(hit.info.path, selectionMode(event));
      }
    };
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointerup", onUp);
    return () => {
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointerup", onUp);
    };
  }, [onPick, onClearPicks, onClearSelection, measuring, rendered, sectionPicking, pickSectionFace]);


  const withViewer = (act: (viewer: Viewer) => void) => {
    const viewer = instance.current?.viewer;
    if (viewer !== undefined && viewer.ready) act(viewer);
  };

  const measure = (on: boolean) => {
    setMeasuring(on);
  };

  // Measuring and exploding exclude each other: the marks and measurements are where the parts are in the model.
  useEffect(() => {
    if (measuring) withViewer((viewer) => viewer.setExplode(false));
  }, [measuring]);

  const explode = (on: boolean) =>
    withViewer((viewer) => {
      if (on) measure(false);
      viewer.setExplode(on);
      // three-cad-viewer readies the explode at its start, for its own slider to drive: show the parts apart.
      if (on) viewer.setRelativeTime(1);
    });


  const captureView = (): SavedViewState | null => {
    const viewer = instance.current?.viewer;
    if (simulationMode.current) return null;
    if (viewer === undefined || !viewer.ready || data === null || shown.current !== data || viewTarget === null) return null;
    const [x, y, z] = viewer.getCameraPosition();
    const states = viewer.getStates();
    return structuredClone({
      target: viewTarget,
      scene: { ...sceneState, hidden: Object.entries(states).filter(([, state]) => state[0] === 0 && state[1] === 0).map(([path]) => path) },
      camera: {
        position: [x, y, z],
        quaternion: viewer.getCameraQuaternion(),
        target: viewer.getCameraTarget(),
        zoom: viewer.getCameraZoom(),
      },
      settings,
      states,
      section: { active: section.active, section: section.section },
      explode: exploded ? viewer.getRelativeTime() : null,
      measuring,
    });
  };

  return (
    <div className={simulation.enabled ? "cad simulation-mode" : "cad"} data-view-mode={simulation.enabled ? "simulation" : "cad"} data-rendered-feedback-stamp={appliedStamp ?? undefined}>
      <div className="cad-view" ref={host} />
      {!simulation.enabled && hovered !== null && (
        <div className="hover-card" ref={card}>
          {hovered.path !== null && describe?.(hovered.path)}
          <Readout hover={hovered} />
        </div>
      )}
      {!simulation.enabled && measuring && data !== null && (
        <>
          <MeasureMarks viewer={viewerOf} measurement={measurementOf(measureState, picked)} />
          <MeasurePanel
            picked={picked}
            state={measureState}
            available={shapes !== null}
            onDrop={onDropPick}
            onClear={onClearPicks}
          />
        </>
      )}
      {!simulation.enabled && section.active && data !== null && <SectionPanel tool={section} viewer={viewerOf} />}
      {simulation.enabled && (liveError !== null || simulation.scene === null || appliedStamp === null) && (
        <div className="simulation-view-note" role="status">
          {liveError ?? simulation.error ?? (simulation.scene === null ? "Loading simulation geometry… Commands and Stop all remain available." : "Waiting for measured link transforms… CAD rest geometry is not live state.")}
        </div>
      )}
      {(data !== null || viewScope !== null) && (
        <ViewToolbar
          settings={settings}
          cadActions={!simulation.enabled}
          onChange={changeSettings}
          onView={(direction) => withViewer((viewer) => viewer.presetCamera(direction))}
          onFit={() => withViewer((viewer) => viewer.reset())}
          measuring={measuring}
          onMeasure={measure}
          exploded={exploded}
          onExplode={explode}
          sectioning={section.active}
          onSection={section.open}
        >
          {!simulation.enabled && viewScope !== null && <SavedViews key={viewScope} scope={viewScope} capture={captureView} restore={onRestoreView} />}
        </ViewToolbar>
      )}
    </div>
  );
}
