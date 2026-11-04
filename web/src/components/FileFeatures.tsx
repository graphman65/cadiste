import type { Load } from "../App";
import { usePersistentSet } from "../persist";
import { treeIndex } from "../model";
import type { TraceData } from "../types";
import { FeatureTree } from "./FeatureTree";
import { Icon } from "./Icon";

interface Props {
  /** The file's parts, in the order the project lists them. */
  parts: string[];
  traces: Record<string, Load<TraceData>>;
  /** The part whose feature is selected, and the feature. */
  selected: { part: string; node: string } | null;
  related: ReadonlySet<string>;
  collapsed: Record<string, ReadonlySet<string>>;
  /** Whether each part shows in the 3D view. */
  visible: (part: string) => boolean;
  onSelect: (part: string, node: string) => void;
  onToggle: (part: string, node: string, open?: boolean) => void;
  onVisibility: (part: string, visible: boolean) => void;
  /** The part under the pointer, to highlight in the view (null: none). */
  onHover: (part: string | null) => void;
  onOpen: (part: string) => void;
}

const NO_NODES: ReadonlySet<string> = new Set();

/** Every part of a file with its features, a group per part: a feature selected here is lit among the others. */
export function FileFeatures(props: Props) {
  const { parts, traces, selected, related, collapsed, visible, onSelect, onToggle, onVisibility, onHover, onOpen } = props;
  const [closed, toggleClosed] = usePersistentSet("cadiste.closed-file-parts");
  return (
    <div className="file-features">
      {parts.map((part) => {
        const state = traces[part];
        const trace = state?.status === "ok" ? state.data : null;
        const open = !closed.has(part);
        const shown = visible(part);
        return (
          <section key={part} className="file-part">
            <div
              className={selected?.part === part ? "file-part-head active" : "file-part-head"}
              onMouseEnter={() => onHover(part)}
              onMouseLeave={() => onHover(null)}
            >
              <button className="file-part-toggle" aria-expanded={open} onClick={() => toggleClosed(part)}>
                <Icon name="chevron" className={open ? "chevron open" : "chevron"} />
                <Icon name="part" className="builder-icon" />
                <span className="part-name">{part}</span>
                {state?.status === "loading" && <span className="spinner" title="Tracing…" />}
                {state?.status === "error" && <span className="part-status error">failed</span>}
                {trace && <span className="part-status">{trace.ops.length} ops</span>}
              </button>
              <button
                className={shown ? "eye" : "eye off"}
                title={shown ? "Hide in the view" : "Show in the view"}
                onClick={() => onVisibility(part, !shown)}
              >
                <Icon name={shown ? "eye" : "eye-off"} />
              </button>
              <button className="open-part" title={`Open ${part} on its own`} onClick={() => onOpen(part)}>
                Open
              </button>
            </div>
            {open && trace && trace.roots.length > 0 && (
              <FeatureTree
                trace={trace}
                index={treeIndex(trace)}
                collapsed={collapsed[part] ?? NO_NODES}
                selected={selected?.part === part ? selected.node : null}
                related={selected?.part === part ? related : NO_NODES}
                onSelect={(node) => onSelect(part, node)}
                onToggle={(node, nodeOpen) => onToggle(part, node, nodeOpen)}
              />
            )}
            {open && trace && trace.roots.length === 0 && (
              <p className="file-part-empty">Made without a builder: no features.</p>
            )}
          </section>
        );
      })}
    </div>
  );
}
