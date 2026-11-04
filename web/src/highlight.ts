export type TokenKind = "keyword" | "string" | "number" | "comment" | "call" | "decorator";

export interface Token {
  text: string;
  kind?: TokenKind;
}

const KEYWORDS = new Set(
  (
    "False None True and as assert async await break case class continue def del elif else except finally for " +
    "from global if import in is lambda match nonlocal not or pass raise return self try while with yield"
  ).split(" "),
);

const TOKEN =
  /(#.*)|("""|''')|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(\b\d[\d_]*(?:\.\d*)?(?:[eE][+-]?\d+)?\b)|([A-Za-z_]\w*)|(@[\w.]+)/g;

/** Rough Python highlighting, line by line (triple-quoted strings carried across lines). */
export function highlightPython(lines: string[]): Token[][] {
  let open: string | null = null; // the triple quote of a string running on from an earlier line
  return lines.map((line) => {
    const tokens: Token[] = [];
    let at = 0;
    if (open !== null) {
      const end = line.indexOf(open);
      if (end < 0) return [{ text: line, kind: "string" }];
      tokens.push({ text: line.slice(0, end + 3), kind: "string" });
      at = end + 3;
      open = null;
    }
    TOKEN.lastIndex = at;
    for (let match = TOKEN.exec(line); match !== null; match = TOKEN.exec(line)) {
      if (match.index > at) tokens.push({ text: line.slice(at, match.index) });
      const [text, comment, triple, string, number, word, decorator] = match;
      at = match.index + text.length;
      if (comment) tokens.push({ text, kind: "comment" });
      else if (triple) {
        const end = line.indexOf(triple, at);
        if (end < 0) {
          tokens.push({ text: line.slice(match.index), kind: "string" });
          open = triple;
          at = line.length;
          break;
        }
        tokens.push({ text: line.slice(match.index, end + 3), kind: "string" });
        at = end + 3;
        TOKEN.lastIndex = at;
      } else if (string) tokens.push({ text, kind: "string" });
      else if (number) tokens.push({ text, kind: "number" });
      else if (word) {
        const kind = KEYWORDS.has(word) ? "keyword" : line[at] === "(" ? "call" : undefined;
        tokens.push({ text, kind });
      } else if (decorator) tokens.push({ text, kind: "decorator" });
    }
    if (at < line.length) tokens.push({ text: line.slice(at) });
    return tokens;
  });
}
