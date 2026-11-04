import { useEffect, useMemo, useRef } from "react";

import { highlightPython } from "../highlight";
import type { Site } from "../types";

interface Props {
  site: Site;
  text: string | undefined;
}

/** The whole file, scrolled to the call, the call's lines lit. */
export function SourceView({ site, text }: Props) {
  const lines = useMemo(() => (text === undefined ? [] : text.replace(/\n$/, "").split("\n")), [text]);
  const tokens = useMemo(() => highlightPython(lines), [lines]);
  const body = useRef<HTMLDivElement>(null);
  const first = site.line;
  const last = site.line + Math.max(site.code.split("\n").length - 1, 0);

  useEffect(() => {
    const target = body.current?.querySelector<HTMLElement>(".line.lit");
    if (target && body.current) body.current.scrollTop = target.offsetTop - body.current.clientHeight / 3;
  }, [site, text]);

  return (
    <div className="source">
      <div className="source-head">
        <span className="source-file">{site.file}</span>
        <span className="source-line">
          line {first}
          {last > first ? `–${last}` : ""}
        </span>
      </div>
      {text === undefined ? (
        <div className="source-missing">Source not available.</div>
      ) : (
        <div className="source-body" ref={body}>
          {tokens.map((line, i) => {
            const number = i + 1;
            const lit = number >= first && number <= last;
            return (
              <div key={number} className={lit ? "line lit" : "line"}>
                <span className="gutter">{number}</span>
                <code>
                  {line.map((token, j) =>
                    token.kind ? (
                      <span key={j} className={`tok-${token.kind}`}>
                        {token.text}
                      </span>
                    ) : (
                      token.text
                    ),
                  )}
                  {line.length === 0 ? " " : null}
                </code>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
