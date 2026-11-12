export type IconName =
  | "box"
  | "cylinder"
  | "extrude"
  | "revolve"
  | "edge"
  | "split"
  | "insert"
  | "mirror"
  | "feature"
  | "part"
  | "assembly"
  | "sketch"
  | "sketch-shape"
  | "line"
  | "folder"
  | "folder-open"
  | "file"
  | "chevron"
  | "fit"
  | "ortho"
  | "perspective"
  | "ghost"
  | "edges"
  | "axes"
  | "grid"
  | "tools"
  | "measure"
  | "explode"
  | "section"
  | "pick-face"
  | "flip"
  | "close"
  | "eye"
  | "eye-off"
  | "solo"
  | "links"
  | "faces"
  | "stack"
  | "code"
  | "panel-hide"
  | "panel-show"
  | "download";

const PATHS: Record<IconName, string> = {
  box: "M3 5.5 8 3l5 2.5v5L8 13l-5-2.5zM3 5.5 8 8l5-2.5M8 8v5",
  cylinder: "M3.5 4.5a4.5 1.8 0 1 0 9 0a4.5 1.8 0 1 0-9 0M3.5 4.5v7a4.5 1.8 0 0 0 9 0v-7",
  extrude: "M2.5 12.5h8l3-2.5h-8zM8 9.5V2.5M5.8 4.7 8 2.5l2.2 2.2",
  revolve: "M12.6 8.6a4.6 4.6 0 1 1-1.4-4.1M11.6 1.8v2.9H8.7",
  edge: "M3 13.5V7l4-4h6.5M3 7h4V3",
  split: "M2 8h12M4 5.5V3h8v2.5M4 10.5V13h8v-2.5",
  insert: "M8 1.5v6M5.6 5.1 8 7.5l2.4-2.4M3 9.5h10v4H3z",
  mirror: "M8 2v12M6 4 2.5 12H6zM10 4l3.5 8H10z",
  feature: "M8 2.5 13.5 8 8 13.5 2.5 8z",
  part: "M3 5.2 8 2.7l5 2.5v5.6l-5 2.5-5-2.5zM3 5.2l5 2.5 5-2.5M8 7.7v5.6",
  // Two parts, one placed beside the other.
  assembly:
    "M1.5 8.3 5 6.6l3.5 1.7v3.9L5 13.9l-3.5-1.7zM1.5 8.3 5 10l3.5-1.7M5 10v3.9M7.5 3.8 11 2.1l3.5 1.7v3.9L11 9.4 8.5 8.2M7.5 3.8 11 5.5l3.5-1.7M11 5.5v3.9M7.5 3.8v2.6",
  sketch: "M1.5 12 4.5 5h10l-3 7zM6.5 9.5h4",
  "sketch-shape": "M4 12.5 3 6l5-3.5L13 6l-1 6.5z",
  line: "M3 12.5 13 3.5M3 12.5h.01M13 3.5h.01",
  folder: "M2 4h4.2l1.5 1.6H14v6.9H2z",
  "folder-open": "M2 12.5V4h4.2l1.5 1.6H13v1.9M2 12.5h10.3L14.5 7.5H4.3z",
  file: "M4 1.5h5.5l3 3v10H4zM9.5 1.5v3h3",
  chevron: "M6 3.5 10.5 8 6 12.5",
  fit: "M2 6V2h4M10 2h4v4M14 10v4h-4M6 14H2v-4",
  ortho: "M3 4h7v7H3zM6 2h7v7M3 4l3-2M10 4l3-2M10 11l3-2",
  perspective: "M2 4.5 8 2l6 2.5v7L8 14l-6-2.5zM8 2v12M2 4.5l6 2.5 6-2.5",
  ghost: "M2 2h8v8H2zM6 6h8v8H6z",
  edges: "M2.5 4.5 8 2l5.5 2.5v7L8 14l-5.5-2.5zM2.5 4.5 8 7l5.5-2.5M8 7v7",
  axes: "M3 13V2.5M3 13h10.5M3 13l5.5-5.5M1.5 4 3 2.5 4.5 4M12 11.5l1.5 1.5-1.5 1.5",
  grid: "M2 2h12v12H2zM2 6h12M2 10h12M6 2v12M10 2v12",
  tools: "M3 4h10M3 8h10M3 12h10M6 2.5v3M10 6.5v3M5 10.5v3",
  measure: "M1.8 10.6 10.6 1.8l3.6 3.6-8.8 8.8zM4.6 7.8l1.4 1.4M6.6 5.8l2 2M8.6 3.8l1.4 1.4",
  explode:
    "M6.2 6.2h3.6v3.6H6.2zM4.8 4.8 2 2M2 4.5V2h2.5M11.2 4.8 14 2M11.5 2H14v2.5M11.2 11.2 14 14M14 11.5V14h-2.5M4.8 11.2 2 14M4.5 14H2v-2.5",
  section:
    "M2.5 4.8 7.5 2.5l6 2.7v6l-5 2.3-6-2.7zM2.5 4.8l6 2.7 5-2.3M8.5 7.5v6M8.5 10.5l3-3M8.5 13.5l5-5M11 12.35l2.5-2.5",
  "pick-face": "M2.5 2.5h8v4M2.5 2.5v8h4M8 8l5.5 2-2.3 1.2L10 13.5z",
  flip: "M2 8h12M5 5.5v-3M3.5 4 5 2.5 6.5 4M11 10.5v3M9.5 12l1.5 1.5 1.5-1.5",
  close: "M4.5 4.5l7 7M11.5 4.5l-7 7",
  eye: "M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8zM8 6a2 2 0 1 0 0 4 2 2 0 1 0 0-4",
  "eye-off": "M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8zM2.5 2.5l11 11",
  solo: "M5 2H2v3M11 2h3v3M14 11v3h-3M5 14H2v-3M5 5h6v6H5z",
  links: "M6.5 9.5l3-3M7 4.5l1.2-1.2a2.5 2.5 0 0 1 3.5 3.5L10.5 8M9 11.5l-1.2 1.2a2.5 2.5 0 0 1-3.5-3.5L5.5 8",
  faces: "M2.5 2.5h4.5v4.5H2.5zM9 2.5h4.5v4.5H9zM2.5 9h4.5v4.5H2.5zM9 9h4.5v4.5H9z",
  stack: "M8 2l6 3-6 3-6-3zM2 8l6 3 6-3M2 11l6 3 6-3",
  code: "M5.5 4.5 2 8l3.5 3.5M10.5 4.5 14 8l-3.5 3.5M9 3 7 13",
  "panel-hide": "M2 3h12v10H2zM10 3v10M5 6l2 2-2 2",
  "panel-show": "M2 3h12v10H2zM10 3v10M7 6 5 8l2 2",
  download: "M8 2v8M4.8 6.8 8 10l3.2-3.2M2.5 11v2.5h11V11",
};

export function Icon({ name, className }: { name: IconName; className?: string }) {
  return (
    <svg
      className={className ? `icon ${className}` : "icon"}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.3}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
