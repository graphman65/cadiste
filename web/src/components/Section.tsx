import { useCallback, useEffect, useRef, useState } from "react";
import type { Viewer } from "three-cad-viewer";

import type { Vec3 } from "../types";
import { Icon } from "./Icon";

/** A plane to cut the scene along: through `point`, its unit `normal` towards the side cut away. */
export interface SectionPlane {
  /** What it was taken from: "XY plane", "Face f3 · bracket". */
  label: string;
  point: Vec3;
  normal: Vec3;
}

/** The section shown: its plane moved `offset` mm along its normal; flipped, the other side is cut away. */
export interface Section {
  plane: SectionPlane;
  offset: number;
  flipped: boolean;
}

const dot = (a: Vec3, b: Vec3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const sub = (a: Vec3, b: Vec3): Vec3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const along = (point: Vec3, direction: Vec3, distance: number): Vec3 => [
  point[0] + direction[0] * distance,
  point[1] + direction[1] * distance,
  point[2] + direction[2] * distance,
];

/** The scene's bounding box, lowest and highest corner. */
function bounds(viewer: Viewer): [Vec3, Vec3] | null {
  // A THREE.Box3, untyped here: three.js ships no types and the app does not depend on it directly.
  const box = viewer.bbox as unknown as Record<"min" | "max", Record<"x" | "y" | "z", number>> | null;
  if (box === null) return null;
  const { min, max } = box;
  return [
    [min.x, min.y, min.z],
    [max.x, max.y, max.z],
  ];
}

/** The corners of the scene's bounding box. */
function corners(viewer: Viewer): Vec3[] {
  const box = bounds(viewer);
  if (box === null) return [];
  const [min, max] = box;
  return [min[0], max[0]].flatMap((x) => [min[1], max[1]].flatMap((y) => [min[2], max[2]].map((z): Vec3 => [x, y, z])));
}

/** The offsets along the plane's normal that span the scene, from cutting nothing away to cutting it all. */
export function offsetRange(viewer: Viewer, plane: SectionPlane): [number, number] {
  const offsets = corners(viewer).map((corner) => dot(plane.normal, sub(corner, plane.point)));
  return offsets.length > 0 ? [Math.min(...offsets), Math.max(...offsets)] : [-1, 1];
}

const AXIS_PLANES: { name: string; normal: Vec3 }[] = [
  { name: "XY", normal: [0, 0, 1] },
  { name: "YZ", normal: [1, 0, 0] },
  { name: "XZ", normal: [0, 1, 0] },
];

/** A principal plane through the scene's centre. */
function axisPlane(viewer: Viewer, name: string, normal: Vec3): SectionPlane {
  const box = bounds(viewer);
  const point: Vec3 = box === null ? [0, 0, 0] : along(box[0], sub(box[1], box[0]), 0.5);
  return { label: `${name} plane`, point, normal };
}

/** "Face f3 · bracket" for `/…/bracket/faces/faces_3`. */
function faceLabel(path: string): string {
  const match = /^(.*)\/faces\/faces_(\d+)$/.exec(path);
  return match ? `Face f${match[2]} · ${match[1].slice(match[1].lastIndexOf("/") + 1)}` : "Face";
}

/**
 * The plane of a flat face, from its mesh as placed in the scene, facing out of the part (the mesh's triangles wind
 * outwards); null for a curved face or one the viewer has no mesh for.
 */
export function planeOfFace(viewer: Viewer, path: string): SectionPlane | null {
  const geometry = viewer.rendered.nestedGroup.meshGeometry.resolve(path);
  if (geometry === null || geometry.topo !== "face" || geometry.indices === undefined) return null;
  const { positions, indices } = geometry;
  const vertex = (i: number): Vec3 => [positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]];
  // The triangles' normals weighted by their area.
  const sum: Vec3 = [0, 0, 0];
  for (let t = 0; t + 2 < indices.length; t += 3) {
    const a = vertex(indices[t]);
    const u = sub(vertex(indices[t + 1]), a);
    const v = sub(vertex(indices[t + 2]), a);
    sum[0] += u[1] * v[2] - u[2] * v[1];
    sum[1] += u[2] * v[0] - u[0] * v[2];
    sum[2] += u[0] * v[1] - u[1] * v[0];
  }
  const length = Math.hypot(...sum);
  if (indices.length < 3 || length === 0) return null;
  const normal: Vec3 = [sum[0] / length, sum[1] / length, sum[2] / length];
  const point = vertex(indices[0]);
  // Flat: every vertex on the plane through the first, to the mesh's float precision.
  let size = 0;
  let off = 0;
  for (const i of indices) {
    const d = sub(vertex(i), point);
    size = Math.max(size, Math.hypot(...d));
    off = Math.max(off, Math.abs(dot(normal, d)));
  }
  if (off > Math.max(size * 1e-4, 1e-6)) return null;
  return { label: faceLabel(path), point, normal };
}

/** three-cad-viewer's planes keep the side their normal points to: n·(q − centre) + value ≥ 0. */
function clipValue(viewer: Viewer, keep: Vec3, through: Vec3): number {
  const center = viewer.rendered.clipping.clipPlanes[0].center as Vec3;
  return -dot(keep, sub(through, center));
}

/** The side kept and a point of the cut. */
function cutOf(section: Section): { keep: Vec3; through: Vec3 } {
  const { normal, point } = section.plane;
  return {
    keep: section.flipped ? normal : [-normal[0], -normal[1], -normal[2]],
    through: along(point, normal, section.offset),
  };
}

/** Cut along the section's plane (three-cad-viewer's first clipping plane), the other two past the scene. */
function place(viewer: Viewer, section: Section) {
  const { keep, through } = cutOf(section);
  viewer.setClipNormal(0, keep, clipValue(viewer, keep, through));
  const box = corners(viewer);
  const margin = 1 + viewer.bb_radius * 0.01;
  const others: [1 | 2, Vec3][] = [
    [1, [0, -1, 0]],
    [2, [0, 0, -1]],
  ];
  for (const [index, normal] of others) {
    // Past the box corner furthest on the side it cuts away: it keeps the whole scene.
    const open = Math.max(...box.map((corner) => clipValue(viewer, normal, corner))) + margin;
    viewer.setClipNormal(index, normal, open);
  }
}

export interface SectionTool {
  /** Whether the section tool is open. */
  active: boolean;
  section: Section | null;
  /** Whether the next click on a face takes its plane. */
  picking: boolean;
  /** Why the last pick took no plane. */
  note: string | null;
  open: (on: boolean) => void;
  setPicking: (on: boolean) => void;
  /** A click on a face while picking. */
  pickFace: (path: string) => void;
  chooseAxis: (name: string, normal: Vec3) => void;
  setOffset: (offset: number) => void;
  flip: () => void;
  /** Replace the cut when switching to a saved view; picking is transient, never restored. */
  restore: (active: boolean, section: Section | null) => void;
}

/**
 * A section of the scene along a plane taken from a flat face or a principal plane, moved along its normal. It
 * stays across renders (a rebuild is cut where the last build was), on until the tool is closed.
 */
export function useSection(viewer: () => Viewer | null, rendered: number): SectionTool {
  const [active, setActive] = useState(false);
  const [section, setSection] = useState<Section | null>(null);
  const [picking, setPicking] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  /** What the viewer was last cut with: a new plane, side or render places all three planes again. */
  const placed = useRef<{ plane: SectionPlane; flipped: boolean; rendered: number } | null>(null);

  const ready = useCallback(() => {
    const current = viewer();
    return current !== null && current.ready ? current : null;
  }, [viewer]);

  useEffect(() => {
    const current = ready();
    if (current === null) return;
    if (section === null) {
      if (placed.current !== null && current.state.get("activeTab") === "clip") current.setActiveTab("tree");
      placed.current = null;
      return;
    }
    const last = placed.current;
    if (last !== null && last.plane === section.plane && last.flipped === section.flipped && last.rendered === rendered) {
      // The cut moved along its normal: its constant only, cheap enough to follow a slider.
      const { keep, through } = cutOf(section);
      current.setClipSlider(0, clipValue(current, keep, through));
    } else {
      place(current, section);
    }
    // Clipping (and its caps) is on while three-cad-viewer's clip tab is; a render or its own toolbar leaves it.
    if (current.state.get("activeTab") !== "clip") current.setActiveTab("clip");
    placed.current = { plane: section.plane, flipped: section.flipped, rendered };
  }, [ready, section, rendered]);

  const take = useCallback((plane: SectionPlane) => {
    setSection({ plane, offset: 0, flipped: false });
    setPicking(false);
    setNote(null);
  }, []);

  // Stable: the view's click handler is bound to it.
  const pickFace = useCallback(
    (path: string) => {
      const current = ready();
      const plane = current === null ? null : planeOfFace(current, path);
      if (plane === null) setNote("That face is not flat: pick a flat one, or a principal plane.");
      else take(plane);
    },
    [ready, take],
  );

  return {
    active,
    section,
    picking,
    note,
    open: (on) => {
      setActive(on);
      setPicking(on);
      setNote(null);
      if (!on) setSection(null);
    },
    setPicking: (on) => {
      setPicking(on);
      setNote(null);
    },
    pickFace,
    chooseAxis: (name, normal) => {
      const current = ready();
      if (current !== null) take(axisPlane(current, name, normal));
    },
    setOffset: (offset) => setSection((current) => current && { ...current, offset }),
    flip: () => setSection((current) => current && { ...current, flipped: !current.flipped }),
    restore: (on, cut) => {
      setActive(on);
      setSection(cut);
      setPicking(false);
      setNote(null);
    },
  };
}

const DIGITS = 3;

/** The section tool's card: the plane to cut along, where along its normal, which side stays. */
export function SectionPanel({ tool, viewer }: { tool: SectionTool; viewer: () => Viewer | null }) {
  const { section, picking, note } = tool;
  const current = viewer();
  const range = section !== null && current !== null && current.ready ? offsetRange(current, section.plane) : null;
  const step = range === null ? 0.01 : Math.max((range[1] - range[0]) / 1000, 1e-4);
  return (
    <section className="section-panel readout" aria-label="Section">
      <div className="readout-head">
        Section
        <span className="spacer" />
        <button className="icon-button" onClick={() => tool.open(false)} title="Close the section">
          <Icon name="close" />
        </button>
      </div>
      <div className="section-planes">
        <button
          className={picking ? "icon-button on" : "icon-button"}
          aria-pressed={picking}
          onClick={() => tool.setPicking(!picking)}
          title="Pick a flat face to cut along"
        >
          <Icon name="pick-face" />
        </button>
        {AXIS_PLANES.map(({ name, normal }) => (
          <button
            key={name}
            className={section?.plane.label === `${name} plane` ? "on" : undefined}
            onClick={() => tool.chooseAxis(name, normal)}
            title={`Cut along the ${name} plane, through the scene's centre`}
          >
            {name}
          </button>
        ))}
      </div>
      {picking && <p className="muted">Click a flat face to cut along its plane.</p>}
      {note !== null && <p className="section-note">{note}</p>}
      {section !== null && (
        <>
          <div className="section-plane">
            <span className="name">{section.plane.label}</span>
            <span className="spacer" />
            <button
              className="icon-button"
              onClick={tool.flip}
              title="Flip: cut away the other side"
            >
              <Icon name="flip" />
            </button>
          </div>
          <div className="section-offset">
            <input
              type="range"
              min={range?.[0] ?? -1}
              max={range?.[1] ?? 1}
              step={step}
              value={section.offset}
              onChange={(event) => tool.setOffset(event.target.valueAsNumber)}
              aria-label="Offset along the normal"
            />
            <input
              type="number"
              step={step}
              value={Number(section.offset.toFixed(DIGITS))}
              onChange={(event) => {
                if (!Number.isNaN(event.target.valueAsNumber)) tool.setOffset(event.target.valueAsNumber);
              }}
              aria-label="Offset along the normal (mm)"
            />
            <span className="unit">mm</span>
          </div>
        </>
      )}
    </section>
  );
}
