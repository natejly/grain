"use client";

import { AlertTriangle, Download, FileWarning, RefreshCw } from "lucide-react";
import type { Diagnostic, Typesetter } from "wasmtex";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export type LatexFileInput = { path: string; content: string };

/** Generated into public/latex by scripts/sync-sandbox-assets.mjs. */
const ASSETS_BASE_URL = "/latex/assets/";
const WORKER_URL = "/latex/worker.js";

/**
 * TeX has no built-in run limit, and a pathological document can spin the worker
 * forever. Thirty seconds is far past a normal compile (a three-pass article
 * with a bibliography runs in well under two) and short enough to stay a
 * hiccup rather than a hang.
 */
const COMPILE_TIMEOUT_MS = 30_000;

/**
 * Much longer than the bundler's 400 ms. A cancelled compile terminates the
 * worker and the next one re-initialises the whole engine, so interrupting a run
 * is expensive here in a way it is not for esbuild — waiting for a real pause in
 * typing is cheaper than racing the user.
 */
const DEFAULT_DEBOUNCE_MS = 1500;

/** Once the last preview is gone, hold the engine briefly: React remounts. */
const DISPOSE_GRACE_MS = 10_000;

export type LatexEngine = "pdftex" | "xetex";

export type LatexOutcome =
  | {
      status: "ok";
      pdf: Uint8Array;
      log: string;
      diagnostics: readonly Diagnostic[];
      passes: number;
      elapsedMs: number;
    }
  | { status: "failed"; message: string; log: string; diagnostics: readonly Diagnostic[] }
  | { status: "cancelled" };

// ---------------------------------------------------------------------------
// Reading what TeX said

/**
 * Emscripten prints this on every run, including successful ones. It is about
 * the wasm runtime, not the document, and it is the last thing in the log —
 * exactly where a user looks first for the real error.
 */
const RUNTIME_NOISE = /keepRuntimeAlive\(\)|but the runtime is not exiting/;

/**
 * Warnings that a multi-pass run resolves on its own. `log` is the transcript of
 * every pass concatenated, so a document whose references came out perfect still
 * carries pass one's complaints that they were undefined. Showing those on a
 * successful compile tells the user their paper is broken when it is not.
 */
const RESOLVED_BY_RERUN =
  /undefined (?:references|citations)|(?:Reference|Citation|LaTeX Warning: Reference) .*undefined|Label\(s\) may have changed|Rerun/i;

/** The transcript with the runtime's own chatter removed. */
export function cleanLog(log: string): string {
  return log
    .split("\n")
    .filter((line) => !RUNTIME_NOISE.test(line))
    .join("\n")
    .trim();
}

/**
 * Files inside the engine's own TeX tree. A warning attributed to one of these
 * is about the distribution, not the document — the starter's graphicx pulls in
 * an epstopdf shell-escape warning nobody can act on — so it is not the user's
 * problem unless the compile actually failed.
 */
const ENGINE_INTERNAL = /^\/texlive\//;

/**
 * The diagnostics worth showing. On a failed compile that is all of them; on a
 * successful one the rerun-resolved warnings are dropped, and what remains is
 * de-duplicated because each surviving pass reported it again.
 */
export function visibleDiagnostics(
  diagnostics: readonly Diagnostic[],
  ok: boolean,
): Diagnostic[] {
  const seen = new Set<string>();
  const kept: Diagnostic[] = [];
  for (const diagnostic of diagnostics) {
    if (ok && diagnostic.severity === "warning") {
      if (RESOLVED_BY_RERUN.test(diagnostic.message)) continue;
      if (diagnostic.file && ENGINE_INTERNAL.test(diagnostic.file)) continue;
    }
    const key = `${diagnostic.severity}|${diagnostic.file ?? ""}|${diagnostic.line ?? ""}|${diagnostic.message}`;
    if (seen.has(key)) continue;
    seen.add(key);
    kept.push(diagnostic);
  }
  return kept;
}

/**
 * The name in TeX's "File `x.sty' not found", if that is why the compile failed.
 *
 * This is the most common failure a user will hit, because the offline bundle
 * carries a core package tier and not the other ~2,400 packages. The raw message
 * reads like a broken install; it is not, and the difference matters.
 */
export function missingPackage(diagnostics: readonly Diagnostic[]): string | null {
  for (const diagnostic of diagnostics) {
    const match = /File `([^']+)\.(sty|cls)' not found/.exec(diagnostic.message);
    if (match) return match[1];
  }
  return null;
}

/** True when the source declares a document class, ignoring commented-out ones. */
export function hasDocumentClass(source: string): boolean {
  const uncommented = source.replace(/(?<!\\)%.*/g, "");
  return /\\documentclass\s*(?:\[[^\]]*\])?\s*\{[^}]+\}/.test(uncommented);
}

/** A sentence a LaTeX user can act on, for each way the engine itself can fail. */
export function describeEngineFailure(caught: unknown): string {
  const error = caught as { name?: string; code?: string; message?: string; detail?: string };
  const detail = error?.detail ? ` (${error.detail})` : "";
  switch (error?.name) {
    case "AssetVersionMismatchError":
      return (
        "The TeX engine and its data files are different versions. " +
        "Re-run `pnpm --filter @workspace/web sandbox-assets` to resync them."
      );
    case "FatalError":
      if (error.code === "unsupported-engine") {
        return "That TeX engine is not available here; use pdfTeX or XeTeX.";
      }
      if (error.code === "init-failed" && /HTTP 404/.test(error.message ?? "")) {
        return (
          "The TeX engine's data files are not installed. Run " +
          "`pnpm --filter @workspace/web sandbox-assets` after downloading them " +
          "(the script prints the command)."
        );
      }
      return `The TeX engine stopped: ${error.message ?? "unknown fatal error"}${detail}`;
    case "WorkerCrashedError":
      return "The TeX engine crashed. Compiling again starts it fresh.";
    case "TypesetInputError":
      return `The compile request was rejected: ${error.message ?? "invalid input"}`;
    default:
      return error?.message ? error.message : String(caught);
  }
}

// ---------------------------------------------------------------------------
// The engine

let typesetterPromise: Promise<Typesetter> | null = null;
let disposeTimer: ReturnType<typeof setTimeout> | null = null;

export type AssetProgressHandler = (loaded: number, total: number) => void;

const progressListeners = new Set<AssetProgressHandler>();

function loadTypesetter(): Promise<Typesetter> {
  if (disposeTimer !== null) {
    clearTimeout(disposeTimer);
    disposeTimer = null;
  }
  if (!typesetterPromise) {
    // Not a static import: the client is ~200 KB and pulls ~79 MiB of TeX behind
    // it, and someone who never opens a LaTeX project should pay none of that.
    typesetterPromise = import("wasmtex")
      .then(({ createTypesetter }) => {
        const seen = new Map<string, number>();
        return createTypesetter({
          assetsBaseUrl: ASSETS_BASE_URL,
          workerUrl: WORKER_URL,
          // Both keys are mandatory — the client spreads `onDemand`
          // unconditionally, so omitting it throws before anything loads. It
          // stays empty on purpose: the on-demand tier is half a gigabyte and
          // is not vendored, which is what keeps this offline.
          bundles: { preload: ["core"], onDemand: [] },
          onAssetProgress: ({ assetId, loadedBytes, totalBytes }) => {
            seen.set(assetId, Math.max(seen.get(assetId) ?? 0, loadedBytes));
            if (!seen.has(`${assetId}:total`)) seen.set(`${assetId}:total`, totalBytes);
            let loaded = 0;
            let total = 0;
            for (const [key, value] of seen) {
              if (key.endsWith(":total")) total += value;
              else loaded += value;
            }
            for (const listener of progressListeners) listener(loaded, total);
          },
        });
      })
      .catch((caught) => {
        // Same self-healing reset as the bundler's: a failed boot must not make
        // every later attempt reject with the same stale promise.
        typesetterPromise = null;
        throw caught;
      });
  }
  return typesetterPromise;
}

/**
 * Let the engine go once nothing is showing it. The wasm module and the TeX tree
 * are ~80 MB of worker memory, which is too much to hold for a view the user has
 * navigated away from — but a React remount is indistinguishable from leaving,
 * so the release waits for the dust to settle.
 */
function releaseTypesetter(): void {
  if (disposeTimer !== null) clearTimeout(disposeTimer);
  disposeTimer = setTimeout(() => {
    disposeTimer = null;
    const pending = typesetterPromise;
    typesetterPromise = null;
    void pending?.then((typesetter) => typesetter.dispose()).catch(() => {});
  }, DISPOSE_GRACE_MS);
}

export type CompileOptions = {
  engine?: LatexEngine;
  timeoutMs?: number;
  onLog?: (line: string) => void;
  onProgress?: AssetProgressHandler;
  /**
   * Abandon this compile. The engine runs one job at a time and queues the rest
   * in FIFO order, so a caller that starts a new compile without retiring the old
   * one does not replace it — it lines up behind it, and every later edit waits
   * out work whose answer is already stale. Aborting settles the run as
   * `{ status: "cancelled" }`.
   */
  signal?: AbortSignal;
};

/**
 * Compile a project's files to a PDF, in this browser, with no network.
 *
 * Never throws for anything the user did: a missing entry file, a document that
 * will not typeset, an engine that will not start all come back as
 * `status: "failed"` with a message and the transcript. A LaTeX user needs the
 * log, so it is always carried even on success.
 */
export async function compileLatex(
  files: LatexFileInput[],
  entryPath: string,
  options: CompileOptions = {},
): Promise<LatexOutcome> {
  const { engine = "pdftex", timeoutMs = COMPILE_TIMEOUT_MS, onLog, onProgress, signal } = options;
  if (signal?.aborted) return { status: "cancelled" };
  const map = new Map(files.map((file) => [file.path, file.content]));
  const source = map.get(entryPath);
  if (source === undefined) {
    return {
      status: "failed",
      message: `Entry file "${entryPath}" is not in the project.`,
      log: "",
      diagnostics: [],
    };
  }
  if (!entryPath.toLowerCase().endsWith(".tex")) {
    return {
      status: "failed",
      message: `"${entryPath}" is not a .tex file, so there is nothing to typeset.`,
      log: "",
      diagnostics: [],
    };
  }
  if (!hasDocumentClass(source)) {
    // Worth catching before booting 79 MiB of TeX: without a class, TeX's own
    // complaint is "Missing \\begin{document}" several lines into a preamble
    // that never ran, which points at the wrong problem.
    return {
      status: "failed",
      message: `${entryPath} has no \\documentclass, so TeX has nothing to typeset. Start it with something like \\documentclass{article}.`,
      log: "",
      diagnostics: [],
    };
  }

  if (onProgress) progressListeners.add(onProgress);
  let typesetter: Typesetter;
  try {
    typesetter = await loadTypesetter();
  } catch (caught) {
    return { status: "failed", message: describeEngineFailure(caught), log: "", diagnostics: [] };
  } finally {
    if (onProgress) progressListeners.delete(onProgress);
  }
  // Booting the engine is the long pole on a first visit; a caller that gave up
  // during it must not then be handed a compile slot it no longer wants.
  if (signal?.aborted) return { status: "cancelled" };

  let timedOut = false;
  let job;
  try {
    job = typesetter.typeset({
      engine,
      entry: entryPath,
      files: Object.fromEntries(map),
      passes: "auto",
      // Runs bibtex8 when the .aux asks for it and makeindex when a non-empty
      // .idx exists — the reruns Overleaf does for you.
      bibliography: "auto",
      index: "auto",
    });
  } catch (caught) {
    // typeset() validates synchronously and throws TypesetInputError.
    return { status: "failed", message: describeEngineFailure(caught), log: "", diagnostics: [] };
  }
  // Kept as it streams so a run that never finishes still has a transcript to
  // show: where TeX stopped is the only clue a hung compile gives you.
  const streamed: string[] = [];
  const stop = () => {
    timedOut = true;
    job.cancel();
  };

  // The engine runs one compile at a time and queues the rest in FIFO order, so
  // a job can sit waiting behind an earlier one — which is exactly what a debounced
  // edit during a slow compile produces. Time spent queued is not time TeX spent
  // on this document, and charging it here fails a perfectly good file with
  // "a runaway macro … is the usual cause". So the budget restarts the moment
  // this job's own transcript begins, which is the first instant TeX is running
  // it. The initial timer is still armed, because a job that never starts at all
  // must not wait forever either.
  let timer = setTimeout(stop, timeoutMs);
  let running = false;
  job.onLog((line) => {
    streamed.push(line);
    if (!running) {
      running = true;
      clearTimeout(timer);
      timer = setTimeout(stop, timeoutMs);
    }
  });
  if (onLog) job.onLog(onLog);

  const abort = () => job.cancel();
  signal?.addEventListener("abort", abort, { once: true });

  try {
    const result = await job.done;
    const log = cleanLog(result.log);
    if (result.ok && result.pdf) {
      return {
        status: "ok",
        pdf: result.pdf,
        log,
        diagnostics: visibleDiagnostics(result.diagnostics, true),
        passes: result.stats.passes,
        elapsedMs: result.stats.elapsedMs,
      };
    }
    const diagnostics = visibleDiagnostics(result.diagnostics, false);
    const missing = missingPackage(diagnostics);
    const first = diagnostics.find((diagnostic) => diagnostic.severity === "error");
    const where = first?.file ? ` (${first.file}${first.line ? `:${first.line}` : ""})` : "";
    return {
      status: "failed",
      message: missing
        ? `The package "${missing}" is not in the offline TeX bundle, so it cannot be used here. ` +
          `Remove it, or add ${missing}.sty to the project and it will be picked up.`
        : first
          ? `${first.message}${where}`
          : `The compile produced no PDF (exit code ${result.exitCode}). The log is below.`,
      log,
      diagnostics,
    };
  } catch (caught) {
    const name = (caught as { name?: string })?.name;
    const log = cleanLog(streamed.join("\n"));
    if (timedOut) {
      const seconds = Math.max(1, Math.round(timeoutMs / 1000));
      return {
        status: "failed",
        message: `The compile ran longer than ${seconds} second${seconds === 1 ? "" : "s"} and was stopped. A runaway macro or an unterminated environment is the usual cause.`,
        log,
        diagnostics: [],
      };
    }
    // A deliberate cancel is not a failure — the caller superseded this run.
    if (name === "CancelledError") return { status: "cancelled" };
    return { status: "failed", message: describeEngineFailure(caught), log, diagnostics: [] };
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

// ---------------------------------------------------------------------------
// The view

export type LatexPreviewProps = {
  files: LatexFileInput[];
  entryPath: string;
  engine?: LatexEngine;
  /** Milliseconds of quiet typing before a recompile. */
  debounceMs?: number;
  /** Names the downloaded file; defaults to the entry file's base name. */
  downloadName?: string;
  className?: string;
};

function pdfFileName(entryPath: string, downloadName?: string): string {
  if (downloadName) return downloadName.endsWith(".pdf") ? downloadName : `${downloadName}.pdf`;
  const base = entryPath.slice(entryPath.lastIndexOf("/") + 1);
  return `${base.replace(/\.tex$/i, "") || "document"}.pdf`;
}

function formatDiagnostic(diagnostic: Diagnostic): string {
  const where = diagnostic.file
    ? `${diagnostic.file}${diagnostic.line ? `:${diagnostic.line}` : ""}: `
    : "";
  return `${where}${diagnostic.message}`;
}

/**
 * Compiles a LaTeX project in the browser and shows the PDF.
 *
 * A failed compile never clears a working PDF: the last one that typeset stays
 * on screen under the error, which is how every LaTeX editor behaves and the
 * only way to compare what you broke against what worked.
 */
export function LatexPreview({
  files,
  entryPath,
  engine = "pdftex",
  debounceMs = DEFAULT_DEBOUNCE_MS,
  downloadName,
  className,
}: LatexPreviewProps) {
  const [pdfUrl, setPdfUrl] = useState("");
  const [error, setError] = useState("");
  const [log, setLog] = useState("");
  const [diagnostics, setDiagnostics] = useState<Diagnostic[]>([]);
  const [phase, setPhase] = useState<"idle" | "starting" | "compiling">("idle");
  const [progress, setProgress] = useState({ loaded: 0, total: 0 });
  const [showLog, setShowLog] = useState(false);
  const [nonce, setNonce] = useState(0);
  const run = useRef(0);
  const urlRef = useRef("");

  // Recompiles key off content, not array identity, so a refetch that returns
  // the same files does not spend a second on TeX.
  const signature = useMemo(
    () => JSON.stringify([entryPath, engine, files.map((file) => [file.path, file.content])]),
    [entryPath, engine, files],
  );

  useEffect(() => {
    const generation = ++run.current;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setPhase("starting");
      setProgress({ loaded: 0, total: 0 });
      const outcome = await compileLatex(files, entryPath, {
        engine,
        signal: controller.signal,
        onProgress: (loaded, total) => {
          if (run.current === generation) setProgress({ loaded, total });
        },
        // The first transcript line is the only signal that the engine finished
        // booting and TeX itself is running — the difference between "this is
        // downloading 79 MiB" and "this is typesetting", which are minutes and
        // milliseconds apart on a first visit.
        onLog: () => {
          if (run.current === generation) setPhase("compiling");
        },
      });
      if (run.current !== generation) return;
      if (outcome.status === "cancelled") return;
      setPhase("idle");
      if (outcome.status === "ok") {
        // Copied into a fresh view: the engine's Uint8Array is typed over
        // ArrayBufferLike, which Blob will not accept, and the copy is a few
        // hundred kilobytes at most.
        const bytes = new Uint8Array(outcome.pdf);
        const url = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
        if (urlRef.current) URL.revokeObjectURL(urlRef.current);
        urlRef.current = url;
        setPdfUrl(url);
        setError("");
      } else {
        setError(outcome.message);
      }
      setLog(outcome.log);
      setDiagnostics([...outcome.diagnostics]);
    }, debounceMs);

    return () => {
      window.clearTimeout(timer);
      // Clearing the debounce only stops a compile that has not begun. One that
      // has must be retired too: the engine takes one job at a time, so leaving
      // it running does not merely waste it — the replacement queues behind it
      // and inherits the wait.
      controller.abort();
    };
    // `files`, `entryPath` and `engine` are all covered by `signature`.
  }, [signature, debounceMs, nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(
    () => () => {
      run.current += 1;
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
      releaseTypesetter();
    },
    [],
  );

  const recompile = useCallback(() => setNonce((value) => value + 1), []);

  const busy = phase !== "idle";
  const status = busy
    ? phase === "compiling"
      ? "Typesetting…"
      : progress.total > 0
        ? `Loading TeX (${Math.round((progress.loaded / progress.total) * 100)}%)`
        : "Starting TeX…"
    : error
      ? "Compile failed"
      : pdfUrl
        ? "Compiled"
        : "Nothing compiled yet";

  return (
    <div className={className || "project-preview"}>
      <div className="project-preview-bar">
        <span className="project-preview-status">{status}</span>
        {log && (
          <button className="ghost-button" onClick={() => setShowLog((open) => !open)}>
            <FileWarning size={13} /> {showLog ? "Hide log" : "Log"}
          </button>
        )}
        {pdfUrl && (
          <a
            className="ghost-button"
            href={pdfUrl}
            download={pdfFileName(entryPath, downloadName)}
          >
            <Download size={13} /> PDF
          </a>
        )}
        <button className="ghost-button" onClick={recompile} disabled={busy}>
          <RefreshCw size={13} /> Recompile
        </button>
      </div>

      {error && (
        <pre className="project-preview-error">
          <AlertTriangle size={13} /> {error}
        </pre>
      )}
      {!error && diagnostics.length > 0 && (
        <pre className="project-preview-error">
          {diagnostics.slice(0, 5).map(formatDiagnostic).join("\n")}
        </pre>
      )}
      {showLog && log && <pre className="project-preview-error">{log}</pre>}

      {pdfUrl ? (
        <iframe className="project-preview-frame" title="PDF preview" src={pdfUrl} />
      ) : (
        <div className="project-preview-empty">
          {error ? "Fix the error above to see the PDF." : "Typesetting the document…"}
        </div>
      )}
    </div>
  );
}
