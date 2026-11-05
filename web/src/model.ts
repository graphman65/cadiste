import type { VisibilityState } from "three-cad-viewer";

import type { IconName } from "./components/Icon";
import type { OpData, SceneData, ShapeNode, TraceData, TreeNode } from "./types";

export interface TreeIndex {
  nodes: Map<string, TreeNode>;
  /** Node id -> its parent's id (roots have none). */
  parent: Map<string, string>;
  /** Builder index -> node id. */
  builderNode: Map<number, string>;
}

export function indexTree(roots: TreeNode[]): TreeIndex {
  const index: TreeIndex = { nodes: new Map(), parent: new Map(), builderNode: new Map() };
  const visit = (node: TreeNode, parent: string | null) => {
    index.nodes.set(node.id, node);
    if (parent !== null) index.parent.set(node.id, parent);
    if (node.type === "builder") index.builderNode.set(node.builder, node.id);
    if (node.type !== "op") node.children.forEach((child) => visit(child, node.id));
  };
  roots.forEach((root) => visit(root, null));
  return index;
}

const INDEXES = new WeakMap<TraceData, TreeIndex>();

/** A trace's tree index, made once per trace (several are shown at a time in a file's view). */
export function treeIndex(trace: TraceData): TreeIndex {
  let index = INDEXES.get(trace);
  if (index === undefined) {
    index = indexTree(trace.roots);
    INDEXES.set(trace, index);
  }
  return index;
}

export function ancestors(index: TreeIndex, id: string): string[] {
  const found: string[] = [];
  for (let at = index.parent.get(id); at !== undefined; at = index.parent.get(at)) found.push(at);
  return found;
}

/** The tree node showing an operation: a child builder's hand-over is shown on that builder's node. */
export function opNodeId(trace: TraceData, op: number): string {
  const child = trace.ops[op].child;
  return child === null ? `o${op}` : `b${child}`;
}

export function builderTitle(trace: TraceData, builder: number): string {
  const record = trace.builders[builder];
  const kind = record.kind.replace(/^Build/, "");
  return record.name ? `${kind} ${record.name}` : `${kind} #${record.index}`;
}

export function opIcon(trace: TraceData, op: OpData): IconName {
  const builderKind = trace.builders[op.builder].kind;
  if (builderKind === "BuildSketch") return "sketch-shape";
  if (builderKind === "BuildLine") return "line";
  const kind = op.kind.toLowerCase();
  if (/chamfer|fillet/.test(kind)) return "edge";
  if (/split/.test(kind)) return "split";
  if (/insert|^add$/.test(kind)) return "insert";
  if (/revolve/.test(kind)) return "revolve";
  if (/mirror/.test(kind)) return "mirror";
  if (/extrude|prism|loft|sweep/.test(kind)) return "extrude";
  if (/cyl|hole|cone|sphere|torus/.test(kind)) return "cylinder";
  if (/box|span|wedge/.test(kind)) return "box";
  return "feature";
}

export function builderIcon(kind: string): IconName {
  if (kind === "BuildSketch") return "sketch";
  if (kind === "BuildLine") return "line";
  return "part";
}

export function countOps(node: TreeNode): number {
  if (node.type === "op") return 1;
  return node.children.reduce((sum, child) => sum + countOps(child), 0);
}

export interface VisibleRow {
  node: TreeNode;
  depth: number;
}

export function visibleRows(roots: TreeNode[], collapsed: ReadonlySet<string>): VisibleRow[] {
  const rows: VisibleRow[] = [];
  const visit = (node: TreeNode, depth: number) => {
    rows.push({ node, depth });
    if (node.type !== "op" && !collapsed.has(node.id)) node.children.forEach((child) => visit(child, depth + 1));
  };
  roots.forEach((root) => visit(root, 0));
  return rows;
}

export function fileName(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

export function location(site: { file: string; line: number }): string {
  return `${fileName(site.file)}:${site.line}`;
}

// ---- what the 3D view shows for a selection ----

export type CameraView = "front" | "top" | "right";

export interface Focus {
  /** Visibility, `[faces, edges]`, of the scene's leaves (the ones not named keep theirs). */
  states: Record<string, VisibilityState>;
  /** Exact component paths or whole leaves to highlight. */
  lit: string[];
  /** Draw everything see-through, so a sketch inside the part shows. */
  ghost: boolean;
  /** Look at a sketch face-on (the preset view closest to its plane's normal). */
  view: CameraView | null;
}

function viewAlong([x, y, z]: [number, number, number]): CameraView {
  const [ax, ay, az] = [Math.abs(x), Math.abs(y), Math.abs(z)];
  if (az >= ax && az >= ay) return "top";
  return ay >= ax ? "front" : "right";
}

/** The sketch a builder draws into: itself, or the sketch enclosing a BuildLine. */
function enclosingSketch(trace: TraceData, builder: number): number | null {
  for (let at: number | null = builder; at !== null; at = trace.builders[at].parent) {
    if (trace.builders[at].kind === "BuildSketch") return at;
  }
  return null;
}

function opsUnder(node: TreeNode, into: number[] = []): number[] {
  if (node.type === "op") into.push(node.op);
  else node.children.forEach((child) => opsUnder(child, into));
  return into;
}

/**
 * - a part operation: its faces lit; a sketch it consumed shown in place, the part ghosted so the sketch shows;
 * - a sketch: shown face-on, the part drawn as edges only around it;
 * - a sketch element: the same, with the element shown on its sketch;
 * - a group or a nested part builder: the faces of every operation in it lit.
 */
export function sceneFocus(trace: TraceData, index: TreeIndex, selected: string | null): Focus {
  const scene = trace.scene;
  const shown = new Set<string>();
  let faces: number[] = [];
  let view: CameraView | null = null;
  let inspecting = false;
  const showSketch = (builder: number | null, faceOn: boolean) => {
    const sketch = builder === null ? undefined : scene.sketches[builder];
    if (sketch === undefined) return;
    shown.add(sketch.path);
    if (faceOn && sketch.normal) view = viewAlong(sketch.normal);
  };

  const node = selected === null ? undefined : index.nodes.get(selected);
  if (node?.type === "op") {
    const op = trace.ops[node.op];
    if (trace.builders[op.builder].kind === "BuildPart") {
      faces = [...op.faces, ...op.via_faces];
      op.inputs.forEach((input) => showSketch(input, false));
    } else {
      showSketch(enclosingSketch(trace, op.builder), true);
      const element = scene.elements[op.index];
      if (element !== undefined) shown.add(element);
      inspecting = true;
    }
  } else if (node?.type === "builder") {
    const record = trace.builders[node.builder];
    if (record.kind === "BuildPart") {
      if (record.parent !== null) faces = opsUnder(node).flatMap((op) => trace.ops[op].faces);
    } else {
      showSketch(enclosingSketch(trace, record.index), true);
      inspecting = true;
    }
  } else if (node?.type === "group") {
    faces = opsUnder(node).flatMap((op) => trace.ops[op].faces);
  }

  // Inspecting a sketch, the part is context: its edges only, so the sketch is not behind its faces.
  const states: Record<string, VisibilityState> = { [scene.part]: inspecting ? [0, 1] : [1, 1] };
  for (const path of [...Object.values(scene.sketches).map((s) => s.path), ...Object.values(scene.elements)]) {
    states[path] = shown.has(path) ? [1, 1] : [0, 0];
  }
  const lit = [...(scene.face_ids ? faces.map((face) => `${scene.part}/faces/faces_${face}`) : []), ...shown];
  if (node?.type === "builder" && trace.builders[node.builder].kind === "BuildPart" && trace.builders[node.builder].parent === null) lit.push(scene.part);
  return { states, lit, ghost: shown.size > 0 && !inspecting, view };
}

/** The tree node to select for a pick in the part's scene: the operation that made a face, a sketch, an element. */
export function nodeForPick(trace: TraceData, path: string): string | null {
  const scene = trace.scene;
  const prefix = `${scene.part}/faces/faces_`;
  if (scene.face_ids && path.startsWith(prefix)) {
    const face = Number(path.slice(prefix.length));
    const op = trace.ops.find((candidate) => candidate.faces.includes(face));
    return op === undefined ? null : opNodeId(trace, op.index);
  }
  for (const [op, element] of Object.entries(scene.elements)) {
    if (path === element || path.startsWith(`${element}/`)) return `o${op}`;
  }
  for (const [builder, sketch] of Object.entries(scene.sketches)) {
    if (path === sketch.path || path.startsWith(`${sketch.path}/`)) return `b${builder}`;
  }
  return null;
}

/** The leaves of a shape tree (an assembly's part instances), in order. */
export function shapeLeaves(node: ShapeNode): ShapeNode[] {
  return node.parts === undefined ? [node] : node.parts.flatMap(shapeLeaves);
}

export function findShape(node: ShapeNode, id: string): ShapeNode | undefined {
  if (node.id === id) return node;
  for (const child of node.parts ?? []) {
    const found = findShape(child, id);
    if (found !== undefined) return found;
  }
  return undefined;
}

/**
 * The assembly: the hidden instances hidden; lit, the selected instance (or every instance of a selected group) and
 * the instances in `also` (every instance of a part).
 */
export function assemblyFocus(
  root: ShapeNode,
  hidden: ReadonlySet<string>,
  selected: string | null,
  also: readonly string[] = [],
): Focus {
  const states: Record<string, VisibilityState> = {};
  for (const leaf of shapeLeaves(root)) states[leaf.id] = hidden.has(leaf.id) ? [0, 0] : [1, 1];
  const node = selected === null ? undefined : findShape(root, selected);
  const lit = node === undefined ? [...also] : [...shapeLeaves(node).map((leaf) => leaf.id), ...also];
  return { states, lit, ghost: false, view: null };
}

/** The assembly instance (tree leaf) a picked component belongs to. */
export function leafOfPick(root: ShapeNode, path: string): string | null {
  const leaf = shapeLeaves(root).find((candidate) => path === candidate.id || path.startsWith(`${candidate.id}/`));
  return leaf?.id ?? null;
}

/** The leaf of a file's scene showing a part: the file's parts are its leaves, named after them. */
export function leafOfPart(root: ShapeNode, part: string): string | null {
  return shapeLeaves(root).find((leaf) => leaf.name === part)?.id ?? null;
}

/**
 * A file's parts viewed together: the hidden ones hidden, and the selected feature of one of them lit as it would be
 * in the part's own view (its faces; the file's scene has no sketches to show). The part's scene and its leaf in the
 * file's scene number its faces alike: the same build of it.
 */
export function fileFocus(
  root: ShapeNode,
  hidden: ReadonlySet<string>,
  feature: { leaf: string; trace: TraceData; selected: string } | null,
): Focus {
  const base = assemblyFocus(root, hidden, null);
  if (feature === null) return base;
  const own = sceneFocus(feature.trace, treeIndex(feature.trace), feature.selected);
  const prefix = feature.trace.scene.part;
  return { ...base, lit: own.lit.filter((path) => path === prefix || path.startsWith(`${prefix}/faces/`)).map((path) => feature.leaf + path.slice(prefix.length)) };
}

/** A path in a file's scene, under a part's leaf, as the same path in the part's own scene. */
export function pathInPart(trace: TraceData, leaf: string, path: string): string {
  return trace.scene.part + path.slice(leaf.length);
}

/** The number of faces in a mesh's `triangles_per_face`: encoded (`{shape: [n], buffer}`) or decoded (a typed array). */
function faceCount(mesh: unknown): number | null {
  if (mesh === null || typeof mesh !== "object" || !("triangles_per_face" in mesh)) return null;
  const counts = mesh.triangles_per_face;
  if (ArrayBuffer.isView(counts) && "length" in counts && typeof counts.length === "number") return counts.length;
  if (counts === null || typeof counts !== "object" || !("shape" in counts) || !Array.isArray(counts.shape)) return null;
  return typeof counts.shape[0] === "number" ? counts.shape[0] : null;
}

/** Faces in the mesh of a scene's leaf, as the viewer numbers them (null when the leaf holds no mesh of its own). */
export function leafFaceCount(data: SceneData, leaf: string): number | null {
  const shape = shapeLeaves(data.shapes).find((candidate) => candidate.id === leaf)?.shape;
  // As served the leaf points into `instances`; once rendered, three-cad-viewer has put the mesh in its place.
  if (shape !== null && typeof shape === "object" && "ref" in shape && typeof shape.ref === "number") {
    return faceCount(data.instances[shape.ref]);
  }
  return faceCount(shape);
}
