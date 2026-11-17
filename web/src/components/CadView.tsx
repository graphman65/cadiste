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
import type { HubCalls, SceneData, SimulationPose, SimulationScene, Vec3 } from "../types";
import { type SceneHover, Readout } from "./HoverCard";
import { MeasureMarks, MeasurePanel, type Picked, measurementOf, useMeasurement } from "./Measure";
import { SectionPanel, useSection } from "./Section";
import { SavedViews, type SavedViewState, type SceneViewState } from "./SavedViews";
import { type ViewDirection, type ViewSettings, ViewToolbar, useViewSettings } from "./ViewToolbar";
import type { CheckVisualization } from "./CheckReview";

export interface ScreenshotDiagnostics {
  suspicious_empty: boolean;
  warnings: string[];
  pixel_sample: { width: number; height: number; distinct_colors: number; opaque_pixels: number; non_background_pixels: number } | null;
}

export interface RendererInspection {
  phase: "initializing" | "empty" | "rendering" | "ready" | "error" | "disposed";
  ready: boolean;
  camera: SavedViewState["camera"] | null;
  section: SavedViewState["section"];
  display: ViewSettings & { explode: number | null; measuring: boolean; simulation: boolean };
  viewport: { width: number; height: number; canvas_width: number; canvas_height: number; context_lost: boolean };
  visibility: Record<string, number[]>;
  visibility_layers: { selected: readonly string[]; highlighted: string[]; check_review: string | null };
  check_review: null | {
    label: string;
    poses: CheckVisualization["poses"];
    transforms: { path: string; recorded_matrix_world: number[]; matrix_world: number[] }[];
  };
  timings: { mounted_at: number; render_started_at: number | null; render_completed_at: number | null; render_ms: number | null; capture_ms: number | null; resize_at: number | null };
  errors: { phase: string; message: string; at: number }[];
  diagnostics: ScreenshotDiagnostics | null;
}

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
  /** Stable across handle refreshes; changes only when the renderer component remounts. */
  readonly identity: symbol;
  /** Resolves once this scene is rendered and focused: at once if it is the one shown. */
  rendered: (data: SceneData) => Promise<void>;
  /** Validate entity paths without changing the selection or measurement mode. */
  unknownPaths: (paths: string[]) => string[];
  /** Light these paths on top of the selection until the next call; returns the paths unknown. */
  highlight: (paths: string[]) => string[];
  view: (params: HubCalls["view"]["params"]) => void;
  /** PNGs of the scene (base64), along each direction (none: as it is), the camera put back after. */
  screenshot: (views: (ViewDirection | "current")[], width?: number) => Promise<HubCalls["screenshot"]["result"]["images"]>;
  /** The viewer path of the face, edge, vertex or object under the pointer (null: none). */
  hovered: () => string | null;
  /** Restore viewer-owned state after the app has opened the saved target and restored its selection. */
  restore: (state: SavedViewState) => void;
  capture: () => SavedViewState | null;
  section: (params: HubCalls["section"]["params"]) => void;
  explode: (amount: number | null) => void;
  /** Read-only evidence, available before the first scene and after a failed render. */
  inspect: () => RendererInspection;
  /** Independent exact-check pose preview. Null restores the original recorded transforms. */
  reviewCheck: (review: CheckVisualization | null) => void;
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

interface FitBox {
  min: { x: number; y: number; z: number };
  max: { x: number; y: number; z: number };
  clone(): FitBox;
  makeEmpty(): FitBox;
  expandByObject(object: unknown, precise?: boolean): FitBox;
}

/** Fit a bounding sphere without importing a second Three.js runtime or changing orientation. */
function fitGeometry(viewer: Viewer, mode: "all" | "visible" | "selected", selected: readonly string[]): void {
  if (viewer.bbox === null) throw new Error("No rendered bounds are available");
  const box = (viewer.bbox as unknown as FitBox).clone().makeEmpty();
  const states = viewer.getStates();
  const groups = viewer.rendered.nestedGroup.groups;
  const wanted = new Set(selected.map((path) => path.replace(/\/(faces|edges|vertices)\/[^/]+$/, "")));
  for (const [path, state] of Object.entries(states)) {
    if (mode === "visible" && state[0] === 0 && state[1] === 0) continue;
    if (mode === "selected" && ![...wanted].some((own) => path === own || path.startsWith(own + "/"))) continue;
    const group = groups[path];
    if (group !== undefined) box.expandByObject(group, true);
  }
  const low = [box.min.x, box.min.y, box.min.z];
  const high = [box.max.x, box.max.y, box.max.z];
  if (![...low, ...high].every(Number.isFinite)) throw new Error(`No ${mode} geometry to fit`);
  const center = low.map((value, i) => (value + high[i]) / 2) as Vec3;
  const radius = Math.max(1e-6, Math.hypot(...high.map((value, i) => value - low[i])) / 2);
  const camera = viewer.rendered.camera;
  const position = viewer.getCameraPosition();
  const target = viewer.getCameraTarget();
  const delta = position.map((value, i) => value - target[i]);
  const distance = Math.hypot(...delta);
  let nextDistance = distance;
  let zoom: number | null = null;
  if (camera.ortho) {
    const area = camera.getVisibleArea();
    zoom = viewer.getCameraZoom() * Math.min(area.width, area.height) / (2.2 * radius);
  } else {
    const perspective = camera.pCamera as unknown as { fov: number; aspect: number };
    const vertical = perspective.fov * Math.PI / 180;
    const horizontal = 2 * Math.atan(Math.tan(vertical / 2) * perspective.aspect);
    nextDistance = 1.1 * radius / Math.sin(Math.min(vertical, horizontal) / 2);
  }
  const next = center.map((value, i) => value + delta[i] / Math.max(distance, 1e-9) * nextDistance) as Vector3Tuple;
  viewer.setCameraLocationSettings(next, null, center, zoom, false);
  viewer.update(true, false);
}

/** Scale once, and diagnose a small deterministic pixel sample; warnings never trigger an implicit retry. */
async function scaled(dataUrl: string, width: number): Promise<{ data: string; diagnostics: ScreenshotDiagnostics }> {
  const image = new Image();
  image.src = dataUrl;
  await image.decode();
  if (image.naturalWidth === 0 || image.naturalHeight === 0) throw new Error("Screenshot has no pixels");
  const scale = Math.min(1, width / image.naturalWidth);
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
  canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
  const context = canvas.getContext("2d");
  if (context === null) throw new Error("Cannot draw the screenshot");
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  const data = canvas.toDataURL("image/png").slice("data:image/png;base64,".length);
  const sample = document.createElement("canvas");
  sample.width = Math.min(64, canvas.width);
  sample.height = Math.min(64, canvas.height);
  const pixels = sample.getContext("2d", { willReadFrequently: true });
  if (pixels === null) throw new Error("Cannot inspect screenshot pixels");
  pixels.drawImage(canvas, 0, 0, sample.width, sample.height);
  const rgba = pixels.getImageData(0, 0, sample.width, sample.height).data;
  const colors = new Map<string, number>();
  let opaque = 0;
  for (let i = 0; i < rgba.length; i += 4) {
    if (rgba[i + 3] > 0) opaque++;
    const color = `${rgba[i] >> 3},${rgba[i + 1] >> 3},${rgba[i + 2] >> 3},${rgba[i + 3] >> 3}`;
    colors.set(color, (colors.get(color) ?? 0) + 1);
  }
  const nonBackground = sample.width * sample.height - Math.max(0, ...colors.values());
  const suspicious = opaque === 0 || nonBackground < 4;
  return { data, diagnostics: {
    suspicious_empty: suspicious,
    warnings: suspicious ? ["Screenshot appears empty or near-uniform. Check camera, clipping, hidden/solo layers and render errors; image retained without retry."] : [],
    pixel_sample: { width: sample.width, height: sample.height, distinct_colors: colors.size, opaque_pixels: opaque, non_background_pixels: nonBackground },
  } };
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
  elements: ArrayLike<number>;
  clone: () => LiveMatrix;
  set: (...values: number[]) => LiveMatrix;
  invert: () => LiveMatrix;
  multiply: (matrix: LiveMatrix) => LiveMatrix;
  decompose: (position: unknown, quaternion: unknown, scale: unknown) => void;
}
interface LiveNode {
  matrix: LiveMatrix;
  matrixWorld: LiveMatrix;
  parent: { matrixWorld: LiveMatrix; updateWorldMatrix: (parents: boolean, children: boolean) => void } | null;
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

/** Fixed world XYZ rotations (Rz * Ry * Rx), then translation, about the exact checker's world pivot. */
function checkMatrix(pose: CheckVisualization["poses"][number], matrix: LiveMatrix): LiveMatrix {
  const [x, y, z] = pose.rotation.map((angle) => (angle % 360) * Math.PI / 180);
  const [sx, sy, sz] = [Math.sin(x), Math.sin(y), Math.sin(z)];
  const [cx, cy, cz] = [Math.cos(x), Math.cos(y), Math.cos(z)];
  const r = [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx,
    sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx, -sy, cy * sx, cy * cx];
  const [ox, oy, oz] = pose.origin;
  const t = pose.translation.map((value, i) => value + pose.origin[i] - r[i * 3] * ox - r[i * 3 + 1] * oy - r[i * 3 + 2] * oz);
  return matrix.set(r[0], r[1], r[2], t[0], r[3], r[4], r[5], t[1], r[6], r[7], r[8], t[2], 0, 0, 0, 1);
}

/**
 * three-cad-viewer (OCP CAD Viewer's renderer) in a div it owns, with the app's own toolbar over it; one viewer,
 * Section-plane picking consumes a click first. Measurement mode keeps its additive picks in the same canonical
 * selection as tree/scene items, so copying never omits measurements or resets unrelated selected items.
 */
export function CadView({ data, focus, hover = null, onPick, describe, shapes = null, selectionPaths, onDropPick, onClearPicks, onClearSelection, viewScope, viewTarget, sceneState, onRestoreView, simulation, ref }: Props) {
  const [identity] = useState(() => Symbol("CadView"));
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
  const waiting = useRef<{ data: SceneData; resolve: () => void; reject: (error: Error) => void }[]>([]);
  const captureCurrent = useRef<() => SavedViewState | null>(() => null);
  const currentSelection = useRef<readonly string[]>([]);
  currentSelection.current = [...(focus?.lit ?? []), ...sceneState.selection.flatMap((item) => item.kind === "feature" ? [] : [item.path])];
  const section = useSection(viewerOf, rendered);
  const { picking: sectionPicking, pickFace: pickSectionFace } = section;
  const pendingView = useRef<SavedViewState | null>(null);
  const simulationMode = useRef(simulation.enabled);
  simulationMode.current = simulation.enabled;
  const liveNodes = useRef<{ scene: SimulationScene; nodes: { link: string; node: LiveNode; restToInitial: LiveMatrix }[]; fitted: boolean } | null>(null);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [appliedStamp, setAppliedStamp] = useState<number | null>(null);
  const lifecycle = useRef<RendererInspection["phase"]>("initializing");
  const timings = useRef<RendererInspection["timings"]>({
    mounted_at: performance.now(), render_started_at: null, render_completed_at: null,
    render_ms: null, capture_ms: null, resize_at: null,
  });
  const errors = useRef<RendererInspection["errors"]>([]);
  const diagnostics = useRef<ScreenshotDiagnostics | null>(null);
  const inspectCurrent = useRef<() => RendererInspection>(() => { throw new Error("Renderer is initializing"); });
  const [renderError, setRenderError] = useState<string | null>(null);
  const [checkReview, setCheckReview] = useState<CheckVisualization | null>(null);
  const checkNodes = useRef<{ node: LiveNode; original: LiveMatrix }[]>([]);
  const checkReviewActive = useRef(false);
  const recordError = (phase: string, error: unknown) => {
    const message = error instanceof Error ? error.message : String(error);
    errors.current.push({ phase, message, at: performance.now() });
    errors.current = errors.current.slice(-32);
    return message;
  };
  const restoreCheck = () => {
    for (const { node, original } of checkNodes.current) {
      node.matrix = original.clone();
      node.matrix.decompose(node.position, node.quaternion, node.scale);
      node.updateMatrixWorld(true);
    }
    checkNodes.current = [];
  };

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
    let display: Display | undefined;
    let viewer: Viewer;
    try {
      display = new Display(element, options);
      viewer = new Viewer(display, options, null);
    } catch (error) {
      display?.dispose();
      lifecycle.current = "error";
      setRenderError(recordError("initialize", error));
      return;
    }
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
    const onError = (event: ErrorEvent) => { recordError("browser", event.message); };
    const onRejection = (event: PromiseRejectionEvent) => { recordError("promise", event.reason); };
    const onContextLost = () => {
      lifecycle.current = "error";
      const message = recordError("webgl", "WebGL context lost; no automatic capture retry was attempted");
      setRenderError(message);
      for (const waiter of waiting.current) waiter.reject(new Error(message));
      waiting.current = [];
    };
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    viewer.renderer.domElement.addEventListener("webglcontextlost", onContextLost);
    const fit = () => {
      if (!viewer.ready) return; // resizeCadView throws before the first render
      const box = element.getBoundingClientRect();
      // three-cad-viewer's toolbar, when shown, sits above the canvas: give the canvas what is left below it.
      const canvas = element.querySelector(".tcv_cad_view_glass, .tcv_cad_view");
      const chrome = canvas ? canvas.getBoundingClientRect().top - box.top : 0;
      viewer.resizeCadView(Math.max(box.width, 200), TREE_WIDTH, Math.max(box.height - chrome, 200), true);
      timings.current.resize_at = performance.now();
    };
    instance.current = { display, viewer, fit };
    const observer = new ResizeObserver(fit);
    observer.observe(element);
    lifecycle.current = "empty";
    return () => {
      stopExplodeSync();
      element.removeEventListener("pointermove", onMove);
      element.removeEventListener("keydown", guardLiveKeys, true);
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
      viewer.renderer.domElement.removeEventListener("webglcontextlost", onContextLost);
      for (const waiter of waiting.current) waiter.reject(new Error("Renderer was disposed before the scene was ready"));
      waiting.current = [];
      lifecycle.current = "disposed";
      observer.disconnect();
      viewer.dispose();
      display.dispose();
      instance.current = null;
    };
  }, []);

  useEffect(() => {
    const current = instance.current;
    if (current === null || data === null) return;
    for (const waiter of waiting.current.filter((waiter) => waiter.data !== data)) waiter.reject(new Error("Navigation changed before the requested scene rendered"));
    waiting.current = waiting.current.filter((waiter) => waiter.data === data);
    liveNodes.current = null;
    checkReviewActive.current = false;
    checkNodes.current = [];
    setCheckReview(null);
    lifecycle.current = "rendering";
    timings.current.render_started_at = performance.now();
    timings.current.render_completed_at = null;
    diagnostics.current = null;
    setRenderError(null);
    shown.current = null;
    try {
      if (current.viewer.ready) current.viewer.clear();
      // render() accepts the instanced format too; its declaration names only the decoded tree.
      current.viewer.render(data as unknown as Shapes, RENDER_OPTIONS, viewerOptions(settingsRef.current));
      current.fit();
      lastView.current = null;
      shown.current = data;
      setRendered((count) => count + 1);
    } catch (error) {
      lifecycle.current = "error";
      const message = recordError("render", error);
      setRenderError(message);
      for (const waiter of waiting.current) waiter.reject(new Error(message));
      waiting.current = [];
    }
  }, [data]);

  useEffect(() => {
    const viewer = instance.current?.viewer;
    if (viewer === undefined || !viewer.ready) return;
    viewer.setPickHandler(!simulation.enabled);
    viewer.setSelectionInput(false);
    if (simulation.enabled) {
      restoreCheck();
      checkReviewActive.current = false;
      setCheckReview(null);
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
    const tools = settings.tools && !simulation.enabled && checkReview === null;
    if (viewer.getTools() !== tools) {
      viewer.showTools(tools);
      current.fit();
    }
  }, [settings, rendered, simulation.enabled, checkReview]);

  useEffect(() => {
    const viewer = instance.current?.viewer;
    if (viewer === undefined || !viewer.ready || focus === null) return;
    const states = { ...focus.states };
    // A review is its own display layer: show the checked pair without rewriting either eye/solo layer.
    for (const path of checkReview?.pair.paths ?? []) states[path] = [1, 1];
    viewer.setStates(states);
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
    if (highlight && (extra.length > 0 || checkReview !== null)) {
      // A component lights exactly; a leaf, all its faces.
      const lit = new Set([...extra, ...(checkReview?.pair.paths ?? [])]);
      for (const info of group.registry.entries()) {
        if (lit.has(info.path) || (info.topo === "face" && lit.has(leafOf(info)))) highlight.setSelected(info.id, true);
      }
    }
    // Turn to a sketch once when it is selected, not on every focus change after (the user may orbit away).
    if (focus.view !== null && focus.view !== lastView.current) viewer.presetCamera(focus.view);
    lastView.current = focus.view;
    viewer.update(true, false);
  }, [focus, settings.transparent, rendered, extra, checkReview]);

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

  // Focus/section effects and two browser frames precede readiness; a nonzero canvas is not proof of a painted scene.
  useEffect(() => {
    if (shown.current === null || lifecycle.current !== "rendering") return;
    const scene = shown.current;
    let frame = requestAnimationFrame(() => {
      frame = requestAnimationFrame(() => {
        if (shown.current !== scene || lifecycle.current !== "rendering") return;
        lifecycle.current = "ready";
        timings.current.render_completed_at = performance.now();
        timings.current.render_ms = timings.current.render_completed_at - (timings.current.render_started_at ?? timings.current.render_completed_at);
        const ready = waiting.current.filter((waiter) => waiter.data === scene);
        waiting.current = waiting.current.filter((waiter) => waiter.data !== scene);
        ready.forEach((waiter) => waiter.resolve());
      });
    });
    return () => cancelAnimationFrame(frame);
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
        return viewer !== undefined && viewer.ready && lifecycle.current === "ready" ? viewer : null;
      };
      const registry = () => readyViewer()?.rendered.nestedGroup.registry.entries() ?? [];
      return {
        identity,
        hovered: () => hoveredPath.current,
        capture: () => captureCurrent.current(),
        inspect: () => inspectCurrent.current(),
        reviewCheck: (review) => {
          if (review === null && !checkReviewActive.current) return;
          const viewer = review === null ? instance.current?.viewer ?? null : readyViewer();
          if (viewer === null) {
            if (review !== null) throw new Error("Nothing is rendered");
            restoreCheck();
            setCheckReview(null);
            return;
          }
          // Validate all paths and pivots before changing even one node.
          for (const path of review?.pair.paths ?? []) {
            if (viewer.rendered.nestedGroup.groups[path] === undefined) throw new Error(`Checked instance is not in this scene: ${path}`);
          }
          const poses = review?.poses ?? [];
          const nodes = poses.map((pose) => {
            if (![pose.translation, pose.rotation, pose.origin].every((vector) => Array.isArray(vector) && vector.length === 3 && vector.every(Number.isFinite))) throw new Error("A pose review needs the checker's finite resolved world origin");
            const node = viewer.rendered.nestedGroup.groups[pose.path] as unknown as LiveNode | undefined;
            if (node === undefined) throw new Error(`Checked instance is not in this scene: ${pose.path}`);
            return { pose, node };
          });
          restoreCheck();
          viewer.setExplode(false);
          setMeasuring(false);
          for (const { pose, node } of nodes) {
            node.updateMatrix();
            const original = node.matrix.clone();
            node.parent?.updateWorldMatrix(true, false);
            const delta = checkMatrix(pose, original.clone());
            const world = node.parent === null ? original.clone() : node.parent.matrixWorld.clone().multiply(original);
            const moved = delta.multiply(world);
            node.matrix = node.parent === null ? moved : node.parent.matrixWorld.clone().invert().multiply(moved);
            node.matrix.decompose(node.position, node.quaternion, node.scale);
            node.updateMatrixWorld(true);
            checkNodes.current.push({ node, original });
          }
          viewer.updateBounds();
          viewer.idPicker?.setDirty();
          viewer.update(true, false);
          setCheckReview(review);
          checkReviewActive.current = review !== null;
        },
        section: ({ active, plane, offset = 0, flipped = false }) => {
          if (!active) { section.restore(false, null); return; }
          if (plane === undefined || plane === null || !Array.isArray(plane.point) || !Array.isArray(plane.normal) ||
              plane.point.length !== 3 || plane.normal.length !== 3 ||
              ![...plane.point, ...plane.normal, offset].every(Number.isFinite)) throw new Error("A section requires a finite plane point, normal and offset");
          const length = Math.hypot(...plane.normal);
          if (length < 1e-12) throw new Error("Section normal cannot be zero");
          section.restore(true, { plane: { ...plane, normal: plane.normal.map((value) => value / length) as Vector3Tuple }, offset, flipped });
        },
        explode: (amount) => {
          if (amount !== null && (!Number.isFinite(amount) || amount < 0 || amount > 1)) throw new Error("Explode amount must be null or 0–1");
          const viewer = readyViewer();
          if (viewer === null) throw new Error("Nothing is rendered");
          setMeasuring(false);
          viewer.setExplode(amount !== null);
          if (amount !== null) viewer.setRelativeTime(amount);
          setExploded(amount !== null);
        },
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
          new Promise((resolve, reject) => {
            if (lifecycle.current === "error") { reject(new Error(errors.current.at(-1)?.message ?? "Renderer failed")); return; }
            if (shown.current === scene && lifecycle.current === "ready") { resolve(); return; }
            const timer = window.setTimeout(() => {
              waiting.current = waiting.current.filter((waiter) => waiter.data !== scene);
              reject(new Error("Requested scene did not render within 240 seconds; no capture was accepted"));
            }, 240000);
            waiting.current.push({
              data: scene,
              resolve: () => { window.clearTimeout(timer); resolve(); },
              reject: (error) => { window.clearTimeout(timer); reject(error); },
            });
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
        view: ({ direction, fit, ortho, transparent, camera }) => {
          const viewer = readyViewer();
          if (viewer === null) throw new Error("Nothing is rendered");
          if (direction !== undefined) viewer.presetCamera(direction);
          if (ortho !== undefined) viewer.setOrtho(ortho);
          if (fit) fitGeometry(viewer, fit === true ? "all" : fit, currentSelection.current);
          if (camera !== undefined) {
            if (![...camera.position, ...camera.quaternion, ...camera.target, camera.zoom].every(Number.isFinite) || camera.zoom <= 0) throw new Error("Camera values must be finite with positive zoom");
            viewer.setCameraLocationSettings(camera.position, null, camera.target, viewer.rendered.camera.ortho ? camera.zoom : null, false);
            viewer.rendered.camera.setQuaternion(camera.quaternion);
            viewer.update(true, false);
          }
          const change = { ...(ortho === undefined ? {} : { ortho }), ...(transparent === undefined ? {} : { transparent }) };
          if (Object.keys(change).length > 0) changeSettings(change);
        },
        screenshot: async (views, width = SCREENSHOT_WIDTH) => {
          const viewer = readyViewer();
          if (viewer === null || shown.current === null) throw new Error("Nothing is rendered");
          if (!Number.isInteger(width) || width < 64 || width > 4096) throw new Error("Capture width must be an integer from 64 to 4096");
          const scene = shown.current;
          const captureStarted = performance.now();
          const [x, y, z] = viewer.getCameraPosition();
          const position: Vec3 = [x, y, z];
          const quaternion = viewer.getCameraQuaternion();
          const target = viewer.getCameraTarget() as Vec3;
          const zoom = viewer.getCameraZoom();
          const images: HubCalls["screenshot"]["result"]["images"] = [];
          let changedCamera = false;
          try {
            for (const view of views.length > 0 ? views : ["current" as const]) {
              if (shown.current !== scene) throw new Error("Navigation changed during capture; images discarded");
              if (view !== "current") {
                changedCamera = true;
                // Presets recenter on the entire scene; retain the requested framing while changing direction.
                viewer.presetCamera(view, null, false);
                const presetPosition = viewer.getCameraPosition() as Vec3;
                const presetTarget = viewer.getCameraTarget() as Vec3;
                const offset = presetPosition.map((value, i) => value - presetTarget[i]);
                const distance = Math.hypot(...position.map((value, i) => value - target[i]));
                const scale = distance / Math.max(Math.hypot(...offset), 1e-9);
                const next = target.map((value, i) => value + offset[i] * scale) as Vec3;
                const orientation = viewer.getCameraQuaternion();
                viewer.setCameraLocationSettings(next, null, target, viewer.rendered.camera.ortho ? zoom : null, false);
                viewer.rendered.camera.setQuaternion(orientation);
                viewer.update(true, false);
              }
              const image = await viewer.getImage(`cadiste-${view}`);
              if (shown.current !== scene) throw new Error("Navigation changed during capture; images discarded");
              if (typeof image.dataUrl !== "string") throw new Error("The view could not be captured");
              const captured = await scaled(image.dataUrl, width);
              if (shown.current !== scene) throw new Error("Navigation changed while encoding capture; images discarded");
              const states = Object.values(viewer.getStates());
              if (states.length === 0 || states.every((state) => state[0] === 0 && state[1] === 0)) {
                captured.diagnostics.suspicious_empty = true;
                captured.diagnostics.warnings.push("No visible scene leaves; inspect hidden and independent solo layers.");
              }
              if (checkReviewActive.current) captured.diagnostics.warnings.push("Image shows an exact-check pose preview, not the build's recorded pose.");
              diagnostics.current = captured.diagnostics;
              const evidence = { view, ...captured };
              images.push(evidence);
            }
          } catch (error) {
            recordError("capture", error);
            throw error;
          } finally {
            timings.current.capture_ms = performance.now() - captureStarted;
            // Never restore a former target's camera onto a target the user navigated to mid-capture.
            if (changedCamera && shown.current === scene) {
              viewer.setCameraLocationSettings(position, null, target, viewer.rendered.camera.ortho ? zoom : null, false);
              viewer.rendered.camera.setQuaternion(quaternion);
              viewer.update(true, false);
            }
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
      if (simulationMode.current || checkReviewActive.current) return;
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
    if (simulationMode.current || checkReviewActive.current) return null;
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
  captureCurrent.current = captureView;
  inspectCurrent.current = () => {
    const viewer = instance.current?.viewer;
    const canvas = viewer?.renderer.domElement as HTMLCanvasElement | undefined;
    const box = host.current?.getBoundingClientRect();
    const ready = viewer !== undefined && viewer.ready && lifecycle.current === "ready";
    return structuredClone({
      phase: lifecycle.current, ready,
      camera: !ready ? null : {
        position: viewer.getCameraPosition() as Vec3,
        quaternion: viewer.getCameraQuaternion(), target: viewer.getCameraTarget() as Vec3, zoom: viewer.getCameraZoom(),
      },
      section: { active: section.active, section: section.section },
      display: {
        ...settings,
        ...(ready ? { ortho: viewer.getOrtho(), axes: viewer.getAxes(), grid: viewer.getGrids()[0], transparent: viewer.getTransparent(), edges: viewer.getBlackEdges(), tools: viewer.getTools() } : {}),
        explode: ready && exploded ? viewer.getRelativeTime() : null, measuring, simulation: simulation.enabled,
      },
      viewport: { width: box?.width ?? 0, height: box?.height ?? 0, canvas_width: canvas?.width ?? 0, canvas_height: canvas?.height ?? 0, context_lost: viewer?.renderer.getContext().isContextLost() ?? false },
      visibility: ready ? viewer.getStates() : {},
      visibility_layers: { selected: currentSelection.current, highlighted: extra, check_review: checkReview?.label ?? null },
      check_review: checkReview === null ? null : {
        label: checkReview.label, poses: checkReview.poses,
        transforms: checkNodes.current.map(({ node, original }, i) => ({
          path: checkReview.poses[i].path,
          recorded_matrix_world: Array.from((node.parent === null ? original : node.parent.matrixWorld.clone().multiply(original)).elements),
          matrix_world: Array.from(node.matrixWorld.elements),
        })),
      },
      timings: timings.current, errors: errors.current, diagnostics: diagnostics.current,
    });
  };

  return (
    <div className={simulation.enabled ? "cad simulation-mode" : "cad"} data-view-mode={simulation.enabled ? "simulation" : "cad"} data-rendered-feedback-stamp={appliedStamp ?? undefined}>
      <div className="cad-view" ref={host} style={{ visibility: rendered === 0 ? "hidden" : undefined }} />
      {renderError !== null && <div className="simulation-view-note" role="alert">Renderer failed: {renderError}</div>}
      {!simulation.enabled && checkReview !== null && (
        <>
          <MeasureMarks viewer={viewerOf} measurement={{ entities: [], results: checkReview.pair.witnesses == null ? [] : [
            { label: "Minimum distance", kind: "length", value: checkReview.pair.minimum_distance ?? 0, line: checkReview.pair.witnesses },
            ...checkReview.pair.witnesses.map((value) => ({ label: "Witness", kind: "point" as const, value })),
          ] }} />
          <div className="simulation-view-note" role="status">{checkReview.label} · discrete exact-check pose preview; no continuous-motion proof.</div>
        </>
      )}
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
          cadActions={!simulation.enabled && checkReview === null}
          onChange={changeSettings}
          onView={(direction) => withViewer((viewer) => viewer.presetCamera(direction))}
          onFit={(mode) => withViewer((viewer) => fitGeometry(viewer, mode, currentSelection.current))}
          hasSelection={sceneState.selection.length > 0}
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
