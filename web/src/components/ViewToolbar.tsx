import { useCallback, useState } from "react";

import { Icon, type IconName } from "./Icon";

export type ViewDirection = "iso" | "front" | "rear" | "left" | "right" | "top" | "bottom";

export interface ViewSettings {
  ortho: boolean;
  transparent: boolean;
  /** Black edges instead of grey ones. */
  edges: boolean;
  axes: boolean;
  grid: boolean;
  /** three-cad-viewer's own toolbar and panels (clipping, measuring, explode, zebra...). */
  tools: boolean;
}

const DEFAULTS: ViewSettings = { ortho: true, transparent: false, edges: false, axes: false, grid: false, tools: false };
const STORAGE_KEY = "cadiste.view";

export function useViewSettings(): [ViewSettings, (change: Partial<ViewSettings>) => void] {
  const [settings, setSettings] = useState<ViewSettings>(() => {
    try {
      return { ...DEFAULTS, ...JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}") };
    } catch {
      return DEFAULTS;
    }
  });
  const change = useCallback((delta: Partial<ViewSettings>) => {
    setSettings((current) => {
      const next = { ...current, ...delta };
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      return next;
    });
  }, []);
  return [settings, change];
}

const VIEWS: { direction: ViewDirection; label: string }[] = [
  { direction: "iso", label: "Iso" },
  { direction: "front", label: "Front" },
  { direction: "rear", label: "Back" },
  { direction: "top", label: "Top" },
  { direction: "bottom", label: "Bottom" },
  { direction: "left", label: "Left" },
  { direction: "right", label: "Right" },
];

const TOGGLES: { key: Exclude<keyof ViewSettings, "ortho">; icon: IconName; title: string }[] = [
  { key: "transparent", icon: "ghost", title: "See-through" },
  { key: "edges", icon: "edges", title: "Black edges" },
  { key: "axes", icon: "axes", title: "Axes" },
  { key: "grid", icon: "grid", title: "Grid (XY)" },
];

interface Props {
  settings: ViewSettings;
  onChange: (change: Partial<ViewSettings>) => void;
  onView: (direction: ViewDirection) => void;
  onFit: () => void;
  /** Whether exact entity selections are also measured. */
  measuring: boolean;
  onMeasure: (measuring: boolean) => void;
  /** Whether the parts are drawn apart, away from the scene's centre. */
  exploded: boolean;
  onExplode: (exploded: boolean) => void;
  /** Whether the section tool is open: the scene cut along a plane. */
  sectioning: boolean;
  onSection: (sectioning: boolean) => void;
}

export function ViewToolbar({
  settings,
  onChange,
  onView,
  onFit,
  measuring,
  onMeasure,
  exploded,
  onExplode,
  sectioning,
  onSection,
}: Props) {
  return (
    <div className="view-toolbar" role="toolbar" aria-label="View">
      <div className="segment">
        {VIEWS.map(({ direction, label }) => (
          <button key={direction} onClick={() => onView(direction)} title={`${label} view`}>
            {label}
          </button>
        ))}
      </div>
      <button className="icon-button" onClick={onFit} title="Reset the view">
        <Icon name="fit" />
      </button>
      <span className="divider" />
      <button
        className="icon-button"
        onClick={() => onChange({ ortho: !settings.ortho })}
        title={settings.ortho ? "Orthographic (click for perspective)" : "Perspective (click for orthographic)"}
      >
        <Icon name={settings.ortho ? "ortho" : "perspective"} />
      </button>
      {TOGGLES.map(({ key, icon, title }) => (
        <button
          key={key}
          className={settings[key] ? "icon-button on" : "icon-button"}
          aria-pressed={settings[key]}
          onClick={() => onChange({ [key]: !settings[key] })}
          title={title}
        >
          <Icon name={icon} />
        </button>
      ))}
      <span className="divider" />
      <button
        className={measuring ? "icon-button on" : "icon-button"}
        aria-pressed={measuring}
        onClick={() => onMeasure(!measuring)}
        title="Measure selected faces, edges and vertices. Click replaces; Ctrl/Cmd-click toggles; Shift-click adds. All picks are included in standalone snapshots (C)."
      >
        <Icon name="measure" />
      </button>
      <button
        className={exploded ? "icon-button on" : "icon-button"}
        aria-pressed={exploded}
        onClick={() => onExplode(!exploded)}
        title="Explode: draw the parts apart, away from the scene's centre"
      >
        <Icon name="explode" />
      </button>
      <button
        className={sectioning ? "icon-button on" : "icon-button"}
        aria-pressed={sectioning}
        onClick={() => onSection(!sectioning)}
        title="Section: cut the scene along a flat face or a principal plane, and move the cut"
      >
        <Icon name="section" />
      </button>
      <span className="divider" />
      <button
        className={settings.tools ? "icon-button on" : "icon-button"}
        aria-pressed={settings.tools}
        onClick={() => onChange({ tools: !settings.tools })}
        title="Viewer tools: clipping, measuring, explode, zebra"
      >
        <Icon name="tools" />
      </button>
    </div>
  );
}
