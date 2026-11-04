import type { ReactNode } from "react";

import type { Target } from "../App";
import type { Project } from "../types";
import { Icon, type IconName } from "./Icon";

interface Props {
  target: Target;
  project: Project | null;
  /** What the build says about it (counts, build time), on the right. */
  children?: ReactNode;
}

/** Atop the 3D view: what is open, its kind (icon and word) and name, after the folders it is in. */
export function ViewBar({ target, project, children }: Props) {
  let icon: IconName;
  let kind: string;
  let folders: string[];
  let name: string;
  if (target.kind === "part") {
    icon = "part";
    kind = "Part";
    folders = (project?.folders[target.name] ?? []).map((step) => step.name);
    name = target.name;
  } else if (target.kind === "assembly") {
    icon = "assembly";
    kind = "Assembly";
    folders = [];
    name = target.name;
  } else {
    // A folder is a file's level or a package's: the level at its depth in any part's folders says which.
    const segments = target.path.split("/");
    const steps = Object.values(project?.folders ?? {}).find((each) =>
      segments.every((segment, at) => each[at]?.name === segment),
    );
    const file = steps?.[segments.length - 1]?.file ?? false;
    icon = file ? "file" : "folder";
    kind = file ? "File" : "Package";
    folders = segments.slice(0, -1);
    name = segments[segments.length - 1];
  }
  return (
    <div className="view-bar">
      <Icon name={icon} className={`view-bar-icon kind-${icon}`} />
      <span className="view-bar-kind">{kind}</span>
      <span className="view-bar-path" title={[...folders, name].join(" / ")}>
        {folders.map((folder) => (
          <span key={folder} className="view-bar-folder">
            {folder} /{" "}
          </span>
        ))}
        <span className="view-bar-name">{name}</span>
      </span>
      <span className="spacer" />
      {children}
    </div>
  );
}
