import type { Target } from "./App";
import type { SavedViewState } from "./components/SavedViews";
import type { SectionPlane } from "./components/Section";
import type { ViewDirection } from "./components/ViewToolbar";

// The JSON `cadiste export` writes (src/cadiste/export.py) and `cadiste serve` hands out.

export type Mode = "add" | "subtract" | "intersect" | "replace" | "private";

export interface Site {
  file: string;
  line: number;
  function: string;
  label: string;
  /** Source of the expression executing at `line` (the call). */
  code: string;
}

export interface Argument {
  name: string;
  value: string;
  /** The expression in the call; "" when the argument was not written in the call. */
  source: string;
}

export interface OpData {
  index: number;
  kind: string;
  mode: Mode;
  builder: number;
  /** Model-code stack, outermost first; the last entry is the call. */
  site: Site[];
  inputs: number[];
  /** For a child builder handing its product over at exit. */
  child: number | null;
  arguments: Argument[];
  /** Faces of the result this operation made. */
  faces: number[];
  /** Faces made inside a builder this operation brought in (an inserted part). */
  via_faces: number[];
}

export interface BuilderData {
  index: number;
  kind: string;
  mode: Mode;
  name: string;
  parent: number | null;
  site: Site[];
  owner: string | null;
  ops: number[];
  published_by: number | null;
  consumed_by: number[];
}

export type TreeNode =
  | { id: string; type: "op"; op: number }
  | { id: string; type: "builder"; builder: number; children: TreeNode[] }
  | {
      id: string;
      type: "group";
      label: string;
      function: string;
      defined_in: string;
      called_at: Site;
      children: TreeNode[];
    };

/** A node of three-cad-viewer's shape tree: a group has `parts`, a leaf does not. */
export interface ShapeNode {
  id: string;
  name: string;
  parts?: ShapeNode[];
  /**
   * A leaf's mesh: `{ref}` into the scene's `instances` as served; three-cad-viewer resolves it in place when it
   * renders, so afterwards the mesh itself. Read it with `leafFaceCount`.
   */
  shape?: unknown;
}

/** three-cad-viewer's `{instances, shapes}`: passed to `Viewer.render` as is. */
export interface SceneData {
  instances: unknown[];
  shapes: ShapeNode;
}

export interface PartScene {
  data: SceneData;
  /** Viewer path of the part. */
  part: string;
  /** Whether the viewer's `faces_<i>` are the part's faces `i` (highlight and pick by face). */
  face_ids: boolean;
  /** Builder index -> the sketch's viewer path and plane normal. */
  sketches: Record<string, { path: string; normal: [number, number, number] | null }>;
  /** Operation index -> viewer path of the sketch element it drew, placed where the sketch is. */
  elements: Record<string, string>;
}

export interface TraceData {
  /** `volume` in mm³ (of its solids), `bbox` its axis-aligned bounds in mm. */
  build_id: string;
  source_revision?: string;
  source_fingerprint?: string;
  result: { type: string; faces: number; attributed: number; volume: number; bbox: { min: Vec3; max: Vec3 } };
  build_seconds: number;
  roots: TreeNode[];
  ops: OpData[];
  builders: BuilderData[];
  sources: Record<string, string>;
  scene: PartScene;
}
export interface Placement {
  reference: string | null;
  definition: string;
  site: { file: string; line: number; end_line?: number; column?: number };
  method: string;
  code: string | null;
  arguments: { name: string; value: unknown }[];
  call_arguments?: { name: string; position?: number; expression: string }[];
  source_revision: string;
}


export interface AssemblyData {
  build_id: string;
  source_revision?: string;
  source_fingerprint?: string;
  /** `part_of`: instance (leaf path) -> the part it is an instance of, when the server could tell. */
  scene: {
    data: SceneData; root: string; part_of?: Record<string, string>;
    ownership?: { status: string; matched: number; total: number; unmatched: string[]; explanation: string };
    instances?: Record<string, { owner: string | null; ownership: "explicit" | "shared_topology" | "unavailable"; placement: Placement | null; placements?: Placement[] }>;
    sources?: Record<string, string>;
    source_revision?: string;
  };
  build_seconds: number;
}

export interface Project {
  target: string;
  parts: string[];
  /** Part name -> its folder, level by level (a package, then a file when it has several); [] lists it at the top. */
  folders: Record<string, { name: string; file: boolean }[]>;
  /** The assemblies' names, in the order `cadiste serve --assembly` gave them. */
  assemblies: string[];
  /** Optional named catalogs; target and folder identities retain their variant prefix. */
  variants?: { id: string; label: string }[];
  default_variant?: string | null;
  /** Assemblies with explicit simulation adapters (`cadiste serve --simulation`). */
  simulations: string[];
  root: string;
  workspace_only?: boolean;
  workspace_url?: string | null;
  project_root?: string;
  profile_path?: string | null;
  check_sets?: Record<string, { target: Target; checks: Record<string, unknown>[]; assertions?: Record<string, unknown>[] }>;
  manufacturing?: ManufacturingConfiguration;
  manufacturing_by_variant?: Record<string, ManufacturingConfiguration>;
}

export interface ManufacturingConfiguration {
  orientations?: Record<string, { rotation: number[]; translation?: number[]; on_bed?: boolean }>;
  groups?: Record<string, string[]>;
  part_orientations?: Record<string, string>;
  bed?: { size: number[]; origin?: number[] };
}

export interface SimulationTarget {
  kind: "assembly";
  name: string;
}

export interface SimulationCommand {
  /** Command input identity: a virtual motor name when transmitted, otherwise a physical joint. */
  joint: string;
  topic: string;
  type: "std_msgs/msg/Float64";
  units: string;
  effort_limit: number;
  velocity_limit: number;
  example: string;
}

/** Ideal, lossless output-coordinate / exposed motor-output-shaft coordinate ratio. */
export interface SimulationTransmission {
  name: string;
  joint: string;
  ratio: number;
}

/** The simulation adapter's control metadata, with the server's snapshot identity. */
export interface SimulationControlsConfig {
  config_id: string;
  target: SimulationTarget;
  model: string;
  commands: SimulationCommand[];
  transmissions: SimulationTransmission[];
  topology: {
    links: { name: string; owner: "robot"; pose: SimulationPose }[];
    joints: { name: string; parent: string; child: string; kind: string; pose: SimulationPose;
      axis: Vec3; axis_frame: "joint"; actuated: boolean; lower: number | null; upper: number | null;
      effort: number | null; velocity: number | null; damping: number; friction: number;
      spring_stiffness: number; spring_reference: number; velocity_gain: number; velocity_integral_gain: number }[];
    environment: { name: string; owner: "environment"; static: true; pose: SimulationPose }[];
  };
  feedback: {
    world_frame: string;
    model_frame: string;
    links: Record<string, { frame: string; rest: SimulationPose }>;
    joints: string[];
    visuals: Record<string, {
      parent: string;
      joint: string;
      multiplier: number;
      axis: [number, number, number];
      pose: SimulationPose;
    }>;
  };
}

/** A publish acknowledgement, not measured motor state. */
export interface SimulationCommandResult {
  published: Record<string, number>;
  failed: Record<string, string>;
}

/** TF poses are absolute in feedback.world_frame; metres and xyzw quaternions. */
export interface SimulationPose {
  position: [number, number, number];
  quaternion: [number, number, number, number];
}

export interface SimulationFeedback {
  status: "waiting" | "live" | "stale" | "error";
  model: string;
  stamp: number | null;
  age: number | null;
  /** Physical joint samples plus explicitly declared ideal motor states derived from them. */
  joints: Record<string, { position: number | null; velocity: number | null; effort: number | null }>;
  model_pose: SimulationPose | null;
  links: Record<string, SimulationPose>;
  missing_frames: string[];
  error?: string;
}

export interface SimulationScene extends AssemblyData {
  model: string;
  config_id: string;
  /** Explicit physical-link and driven-visual nodes; rest positions are CAD millimetres, not metres. */
  links: Record<string, { path: string; rest: SimulationPose }>;
}

export interface TraceError {
  error: string;
  log?: string;
}

// What `POST /api/measure` answers (src/cadiste/measure.py), in millimetres and degrees.

export type Vec3 = [number, number, number];

export interface Quantity {
  label: string;
  kind: "length" | "area" | "angle" | "point" | "direction";
  value: number | Vec3;
  /** The segment a distance spans, to draw it. */
  line?: [Vec3, Vec3];
}

export interface MeasuredEntity {
  path: string;
  topo: "face" | "edge" | "vertex";
  /** Its geometry: "plane", "cylinder", "line", "circle", "arc", "point"... */
  kind: string;
  properties: Quantity[];
}

export interface Measurement {
  entities: MeasuredEntity[];
  /** What relates the entities: distances and angle between two, total length or area of several. */
  results: Quantity[];
}

// The hub `cadiste serve` runs: harness sessions linked to a viewer tab, which they read and drive.

export interface SessionInfo {
  id: string;
  /** The harness: "claude-code". */
  client: string;
  /** A word pair ("amber-fox") the harness shows its user too, to tell sessions apart. */
  code: string;
  /** The tab the session is linked to. */
  tab: string | null;
  /** Waiting for the user to link it to a tab. */
  requesting: boolean;
  alive?: boolean;
  last_seen?: number;
  created?: number;
}

/** A captured selection retains exact entity paths and feature provenance across parts. */
export type SelectionItem =
  | { kind: "feature"; part: string; node: string }
  | { kind: "instance"; path: string }
  | { kind: "entity"; path: string };

/** What a tab shows, pushed to the hub as it changes. */
export interface TabState {
  target: Target | null;
  build_id: string | null;
  status: "none" | "loading" | "ok" | "error";
  error: string | null;
  /** The build's shapes file (`Built.shapes`). */
  shapes: string | null;
  selection: SelectionItem[];
  /** Loaded part traces, pinned to the build that supplied their feature/source context. */
  partShapes: Record<string, string>;
  /** Hidden instance leaves. */
  hidden: string[];
  renderer?: unknown;
  visibility_layers?: {
    parts: { hidden: string[]; solo: string | null };
    instances: { hidden: string[]; solo: string | null };
    effective_hidden: string[];
  };
}

export interface BuildIdentity { target: Target; build_id: string; source_revision?: string; source_fingerprint?: string }

export interface CaptureArrangement {
  selection?: SelectionItem[];
  hidden?: string[];
  direction?: ViewDirection;
  fit?: boolean | "all" | "visible" | "selected";
  ortho?: boolean;
  transparent?: boolean;
  camera?: SavedViewState["camera"];
  section?: { active: boolean; plane?: SectionPlane | null; offset?: number; flipped?: boolean };
  explode?: number | null;
}

export interface CaptureResult extends BuildIdentity {
  images: {
    view: ViewDirection | "current"; data: string;
    diagnostics?: {
      suspicious_empty: boolean; warnings: string[];
      pixel_sample: { width: number; height: number; distinct_colors: number; opaque_pixels: number; non_background_pixels: number } | null;
    };
  }[];
  timings?: Record<string, number>;
}

/** Renderer transactions are target/build guarded and serialized by the page. */
export interface HubCalls {
  inspect: { params: Record<string, never>; result: object };
  open: {
    params: BuildIdentity & { select?: string | null };
    result: BuildIdentity & { status: "ok" };
  };
  capture: {
    params: BuildIdentity & { select?: string | null; views: (ViewDirection | "current")[]; arrangement?: CaptureArrangement | null; baseline?: string };
    result: CaptureResult;
  };
  select: { params: { items: SelectionItem[] }; result: object };
  highlight: { params: { paths: string[] }; result: object };
  view: {
    params: { direction?: ViewDirection; fit?: boolean | "all" | "visible" | "selected"; ortho?: boolean; transparent?: boolean; camera?: SavedViewState["camera"] };
    result: object;
  };
  section: { params: { active: boolean; plane?: SectionPlane | null; offset?: number; flipped?: boolean }; result: object };
  explode: { params: { amount: number | null }; result: object };
  saved_views: { params: { action: "list" | "create" | "update" | "delete" | "restore"; id?: string; name?: string; state?: SavedViewState }; result: object };
  visibility: { params: { instances: string[]; visible: boolean }; result: object };
  screenshot: {
    params: { views: (ViewDirection | "current")[]; width?: number; arrangement?: CaptureArrangement | null };
    result: CaptureResult;
  };
}

export type HubMethod = keyof HubCalls;

/** A call as the hub sends it; answered with `POST /api/tabs/<tab>/reply`. */
export interface HubRequest {
  id: string;
  method: string;
  params: unknown;
}
