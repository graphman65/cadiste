import type { Mode } from "../types";

const SYMBOL: Record<Mode, string> = { add: "+", subtract: "−", intersect: "∩", replace: "↻", private: "·" };

export function ModeBadge({ mode, long = false }: { mode: Mode; long?: boolean }) {
  return (
    <span className={`mode mode-${mode}`} title={mode}>
      {long ? `${SYMBOL[mode]} ${mode}` : SYMBOL[mode]}
    </span>
  );
}
