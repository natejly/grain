/**
 * A pure client-side line differ emitting a standard unified diff — the
 * format `ProposalDiff` already parses (`---`/`+++` file lines, `@@` hunk
 * headers, ` `/`-`/`+` bodies) — so the history stepper reuses the one
 * existing diff renderer instead of growing a second one.
 *
 * LCS over lines with the usual unified-diff grouping: 3 context lines, and
 * hunks whose context would overlap are merged into one (an equal run has to
 * exceed twice the context to split them, difflib's rule). Identical inputs
 * return "" — the caller says "no changes" in words, not with an empty diff.
 *
 * Guards, because version bodies are unbounded: above ~20k total lines, or
 * when the changed middle would need a DP table past ~16M cells, the diff
 * falls back to a whole-file replacement (every old line `-`, every new line
 * `+`) — still a valid unified diff, just without minimality. A pathological
 * history steps slower, it never hangs the tab.
 */

export type DiffLabels = { from: string; to: string };

const CONTEXT = 3;
const MAX_TOTAL_LINES = 20_000;
const MAX_DP_CELLS = 16_000_000;

type Op = {
  tag: "equal" | "delete" | "insert";
  i1: number;
  i2: number;
  j1: number;
  j2: number;
};

/** "" is zero lines, not one empty line — the oldest version diffs as all-adds. */
function linesOf(text: string): string[] {
  return text === "" ? [] : text.split("\n");
}

/** Opcodes over the changed middle via LCS backtrack, or null when the DP
 *  table would be too large to build. */
function middleOps(a: string[], b: string[], offsetA: number, offsetB: number): Op[] | null {
  const n = a.length;
  const m = b.length;
  if ((n + 1) * (m + 1) > MAX_DP_CELLS) return null;
  const width = m + 1;
  const dp = new Int32Array((n + 1) * width);
  for (let i = 1; i <= n; i += 1) {
    for (let j = 1; j <= m; j += 1) {
      dp[i * width + j] =
        a[i - 1] === b[j - 1]
          ? dp[(i - 1) * width + (j - 1)] + 1
          : Math.max(dp[(i - 1) * width + j], dp[i * width + (j - 1)]);
    }
  }
  // Walk back, collecting single-step ops newest-first, then coalesce.
  const steps: Op["tag"][] = [];
  let i = n;
  let j = m;
  while (i > 0 && j > 0) {
    if (a[i - 1] === b[j - 1]) {
      steps.push("equal");
      i -= 1;
      j -= 1;
    } else if (dp[i * width + (j - 1)] >= dp[(i - 1) * width + j]) {
      // Inserts first on the backward walk, so after the reverse a
      // replacement reads conventionally: its `-` lines before its `+` lines.
      steps.push("insert");
      j -= 1;
    } else {
      steps.push("delete");
      i -= 1;
    }
  }
  while (j > 0) {
    steps.push("insert");
    j -= 1;
  }
  while (i > 0) {
    steps.push("delete");
    i -= 1;
  }
  steps.reverse();
  const ops: Op[] = [];
  let ai = 0;
  let bj = 0;
  for (const tag of steps) {
    const di = tag === "insert" ? 0 : 1;
    const dj = tag === "delete" ? 0 : 1;
    const last = ops[ops.length - 1];
    if (last && last.tag === tag) {
      last.i2 += di;
      last.j2 += dj;
    } else {
      ops.push({
        tag,
        i1: offsetA + ai,
        i2: offsetA + ai + di,
        j1: offsetB + bj,
        j2: offsetB + bj + dj,
      });
    }
    ai += di;
    bj += dj;
  }
  return ops;
}

/** Full opcode cover of both line lists, common prefix/suffix pre-trimmed. */
function opcodes(a: string[], b: string[]): Op[] | null {
  let prefix = 0;
  const max = Math.min(a.length, b.length);
  while (prefix < max && a[prefix] === b[prefix]) prefix += 1;
  let suffix = 0;
  while (
    suffix < max - prefix &&
    a[a.length - 1 - suffix] === b[b.length - 1 - suffix]
  ) {
    suffix += 1;
  }
  const middle = middleOps(
    a.slice(prefix, a.length - suffix),
    b.slice(prefix, b.length - suffix),
    prefix,
    prefix,
  );
  if (middle === null) return null;
  const ops: Op[] = [];
  if (prefix > 0) ops.push({ tag: "equal", i1: 0, i2: prefix, j1: 0, j2: prefix });
  ops.push(...middle);
  if (suffix > 0) {
    ops.push({
      tag: "equal",
      i1: a.length - suffix,
      i2: a.length,
      j1: b.length - suffix,
      j2: b.length,
    });
  }
  return ops;
}

/** difflib's grouped-opcodes rule: trim outer context to CONTEXT lines, and
 *  split only on an equal run longer than twice the context — which is
 *  exactly what merges overlapping hunks into one. */
function grouped(ops: Op[]): Op[][] {
  const codes = ops.map((op) => ({ ...op }));
  const first = codes[0];
  if (first && first.tag === "equal") {
    first.i1 = Math.max(first.i1, first.i2 - CONTEXT);
    first.j1 = Math.max(first.j1, first.j2 - CONTEXT);
  }
  const last = codes[codes.length - 1];
  if (last && last.tag === "equal") {
    last.i2 = Math.min(last.i2, last.i1 + CONTEXT);
    last.j2 = Math.min(last.j2, last.j1 + CONTEXT);
  }
  const groups: Op[][] = [];
  let group: Op[] = [];
  for (const code of codes) {
    if (code.tag === "equal" && code.i2 - code.i1 > 2 * CONTEXT) {
      group.push({
        tag: "equal",
        i1: code.i1,
        i2: Math.min(code.i2, code.i1 + CONTEXT),
        j1: code.j1,
        j2: Math.min(code.j2, code.j1 + CONTEXT),
      });
      groups.push(group);
      group = [
        {
          tag: "equal",
          i1: Math.max(code.i1, code.i2 - CONTEXT),
          i2: code.i2,
          j1: Math.max(code.j1, code.j2 - CONTEXT),
          j2: code.j2,
        },
      ];
      continue;
    }
    group.push(code);
  }
  if (group.length > 0 && !(group.length === 1 && group[0].tag === "equal")) {
    groups.push(group);
  }
  return groups;
}

/** `@@ -a,b +c,d @@` — 1-based starts, and a zero-count range anchors on the
 *  line before it, the unified-diff convention `patch` expects. */
function hunkHeader(group: Op[]): string {
  const i1 = group[0].i1;
  const i2 = group[group.length - 1].i2;
  const j1 = group[0].j1;
  const j2 = group[group.length - 1].j2;
  const aStart = i2 - i1 === 0 ? i1 : i1 + 1;
  const bStart = j2 - j1 === 0 ? j1 : j1 + 1;
  return `@@ -${aStart},${i2 - i1} +${bStart},${j2 - j1} @@`;
}

/**
 * A unified diff of two texts, "" when they are identical. `labels` names
 * the `---`/`+++` lines; the defaults are honest when the caller has nothing
 * better to say.
 */
export function unifiedDiffLines(
  before: string,
  after: string,
  labels: DiffLabels = { from: "before", to: "after" },
): string {
  if (before === after) return "";
  const a = linesOf(before);
  const b = linesOf(after);
  const out: string[] = [`--- ${labels.from}`, `+++ ${labels.to}`];
  const ops = a.length + b.length > MAX_TOTAL_LINES ? null : opcodes(a, b);
  if (ops === null) {
    // The documented fallback: a whole-file replacement instead of the DP.
    out.push(`@@ -${a.length === 0 ? 0 : 1},${a.length} +${b.length === 0 ? 0 : 1},${b.length} @@`);
    for (const line of a) out.push(`-${line}`);
    for (const line of b) out.push(`+${line}`);
    return out.join("\n");
  }
  for (const group of grouped(ops)) {
    out.push(hunkHeader(group));
    for (const op of group) {
      if (op.tag === "equal") {
        for (let i = op.i1; i < op.i2; i += 1) out.push(` ${a[i]}`);
      } else if (op.tag === "delete") {
        for (let i = op.i1; i < op.i2; i += 1) out.push(`-${a[i]}`);
      } else {
        for (let j = op.j1; j < op.j2; j += 1) out.push(`+${b[j]}`);
      }
    }
  }
  return out.join("\n");
}
