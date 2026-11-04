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

import type { Focus } from "../model";
import type { HubCalls, SceneData } from "../types";
import { type SceneHover, Readout } from "./HoverCard";
import { MeasureMarks, MeasurePanel, type Picked, measurementOf, useMeasurement } from "./Measure";
import { SectionPanel, useSection } from "./Section";
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

/** The scene's operations a harness session drives, through the hub. */
export interface CadViewHandle {
  /** Resolves once this scene is rendered and focused: at once if it is the one shown. */
  rendered: (data: SceneData) => Promise<void>;
  /** Measure exactly these faces, edges, vertices (measure mode on); [] drops the picks. Returns the paths unknown. */
  setPicks: (paths: string[]) => string[];
  /** Light these paths on top of the selection until the next call; returns the paths unknown. */
  highlight: (paths: string[]) => string[];
  view: (params: HubCalls["view"]["params"]) => void;
  /** PNGs of the scene (base64), along each direction (none: as it is), the camera put back after. */
  screenshot: (views: ViewDirection[], width?: number) => Promise<HubCalls["screenshot"]["result"]["images"]>;
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
  onPick?: (path: string) => void;
  /** What a hovered component belongs to (the feature that made it, the instance), shown above its geometry. */
  describe?: (path: string) => ReactNode;
  /** The build's shapes file, to measure on (`Built.shapes`); null: this build cannot be measured. */
  shapes?: string | null;
  /** The measure-mode picks, as they change: viewer paths. */
  onPicks?: (paths: string[]) => void;
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

/**
 * three-cad-viewer (OCP CAD Viewer's renderer) in a div it owns, with the app's own toolbar over it; one viewer,
 * re-rendered as data changes. Its native toolbar and panels stay hidden unless the tools toggle asks for them.
 * In measure mode a click picks a face, edge or vertex to measure (again: drops it) instead of selecting; the section
 * tool's pick takes a click on a flat face before either.
 */
export function CadView({ data, focus, hover = null, onPick, describe, shapes = null, onPicks, ref }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const instance = useRef<Instance | null>(null);
  const [rendered, setRendered] = useState(0);
  const lastView = useRef<string | null>(null);
  const [settings, changeSettings] = useViewSettings();
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  const [hovered, setHovered] = useState<SceneHover | null>(null);
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
    // three-cad-viewer reads out what the pointer is over (or a copied point) on a status line under the canvas,
    // set as the hovered component changes; the hover card shows it instead.
    display.setStatusLine = (readout: string) => {
      const picked = viewer.lastObject;
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
      observer.disconnect();
      viewer.dispose();
      display.dispose();
      instance.current = null;
    };
  }, []);

  useEffect(() => {
    const current = instance.current;
    if (current === null || data === null) return;
    if (current.viewer.ready) current.viewer.clear();
    // render() takes the instanced format too (it decodes it first), but its type only names the decoded tree.
    const shapes = data as unknown as Shapes;
    current.viewer.render(shapes, RENDER_OPTIONS, viewerOptions(settingsRef.current));
    current.fit();
    lastView.current = null;
    setPicked([]); // a new build: its components are numbered anew
    shown.current = data;
    setRendered((count) => count + 1);
  }, [data]);

  useEffect(() => {
    const current = instance.current;
    if (current === null || !current.viewer.ready) return;
    const { viewer } = current;
    viewer.setOrtho(settings.ortho);
    viewer.setAxes(settings.axes);
    viewer.setGrids([settings.grid, false, false]);
    viewer.setBlackEdges(settings.edges);
    if (viewer.getTools() !== settings.tools) {
      viewer.showTools(settings.tools);
      current.fit();
    }
  }, [settings, rendered]);

  useEffect(() => {
    const viewer = instance.current?.viewer;
    if (viewer === undefined || !viewer.ready || focus === null) return;
    viewer.setStates(focus.states);
    viewer.setTransparent(focus.ghost || settings.transparent);
    const group = viewer.rendered.nestedGroup;
    const highlight = group.highlight;
    highlight?.clear();
    if (highlight && measuring) {
      // Measuring: what is picked lights up, not the selection's faces.
      for (const pick of picked) highlight.setSelected(pick.id, true);
    } else if (highlight && focus.lit.length > 0) {
      // A lit path is a face (exact) or a whole leaf (its faces).
      const lit = new Set(focus.lit);
      for (const info of group.registry.entries()) {
        if (info.topo !== "face") continue;
        const leaf = info.path.slice(0, info.path.lastIndexOf("/faces/"));
        if (lit.has(info.path) || lit.has(leaf)) highlight.setSelected(info.id, true);
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
  }, [focus, settings.transparent, rendered, measuring, picked, extra]);

  // After the focus effect: a scene waited for is focused too once this runs.
  useEffect(() => {
    const ready = waiting.current.filter((waiter) => waiter.data === shown.current);
    waiting.current = waiting.current.filter((waiter) => waiter.data !== shown.current);
    ready.forEach((waiter) => waiter.resolve());
  }, [rendered]);

  const pickedPaths = picked.map((pick) => pick.path).join("\n");
  useEffect(() => onPicks?.(pickedPaths === "" ? [] : pickedPaths.split("\n")), [pickedPaths, onPicks]);

  useImperativeHandle(
    ref,
    (): CadViewHandle => {
      const readyViewer = () => {
        const viewer = instance.current?.viewer;
        return viewer !== undefined && viewer.ready ? viewer : null;
      };
      const registry = () => readyViewer()?.rendered.nestedGroup.registry.entries() ?? [];
      return {
        rendered: (scene) =>
          new Promise((resolve) => {
            if (shown.current === scene) resolve();
            else waiting.current.push({ data: scene, resolve });
          }),
        setPicks: (paths) => {
          const found = new Map<string, Picked>();
          for (const info of registry()) {
            if (info.topo !== "solid") found.set(info.path, { path: info.path, id: info.id, topo: info.topo });
          }
          const unknown = paths.filter((path) => !found.has(path));
          if (unknown.length > 0) return unknown;
          if (paths.length > 0) setMeasuring(true);
          setPicked(paths.flatMap((path) => found.get(path) ?? []));
          return [];
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
    [changeSettings],
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
        if (hit) onPick?.(hit.info.path);
      } else if (!hit) {
        setPicked([]); // a click on nothing drops every pick
      } else if (hit.info.topo !== "solid") {
        const pick = { path: hit.info.path, id: hit.id, topo: hit.info.topo };
        setPicked((current) =>
          current.some((p) => p.path === pick.path) ? current.filter((p) => p.path !== pick.path) : [...current, pick],
        );
      }
    };
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointerup", onUp);
    return () => {
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointerup", onUp);
    };
  }, [onPick, measuring, rendered, sectionPicking, pickSectionFace]);

  useEffect(() => {
    if (!measuring) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPicked([]);
    };
    // Captured: three-cad-viewer stops the keys it has shortcuts for (Escape among them) inside its view.
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [measuring]);

  const withViewer = (act: (viewer: Viewer) => void) => {
    const viewer = instance.current?.viewer;
    if (viewer !== undefined && viewer.ready) act(viewer);
  };

  const measure = (on: boolean) => {
    setMeasuring(on);
    if (!on) setPicked([]);
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

  return (
    <div className="cad">
      <div className="cad-view" ref={host} />
      {hovered !== null && (
        <div className="hover-card" ref={card}>
          {hovered.path !== null && describe?.(hovered.path)}
          <Readout hover={hovered} />
        </div>
      )}
      {measuring && data !== null && (
        <>
          <MeasureMarks viewer={viewerOf} measurement={measurementOf(measureState, picked)} />
          <MeasurePanel
            picked={picked}
            state={measureState}
            available={shapes !== null}
            onDrop={(path) => setPicked((current) => current.filter((p) => p.path !== path))}
            onClear={() => setPicked([])}
          />
        </>
      )}
      {section.active && data !== null && <SectionPanel tool={section} viewer={viewerOf} />}
      {data !== null && (
        <ViewToolbar
          settings={settings}
          onChange={changeSettings}
          onView={(direction) => withViewer((viewer) => viewer.presetCamera(direction))}
          onFit={() => withViewer((viewer) => viewer.reset())}
          measuring={measuring}
          onMeasure={measure}
          exploded={exploded}
          onExplode={explode}
          sectioning={section.active}
          onSection={section.open}
        />
      )}
    </div>
  );
}
