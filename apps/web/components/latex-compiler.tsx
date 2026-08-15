"use client";

import { AlertTriangle, Download, FileWarning, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "./api";

export type LatexFileInput = { path: string; content: string };

const DEFAULT_DEBOUNCE_MS = 1500;

export type LatexEngine = "pdftex" | "xetex";

export type LatexOutcome =
  | {
      status: "ok";
      pdf: Uint8Array;
      log: string;
    }
  | { status: "failed"; message: string; log: string }
  | { status: "cancelled" };

// ---------------------------------------------------------------------------
// Log helpers

const RESOLVED_BY_RERUN =
  /undefined (?:references|citations)|(?:Reference|Citation|LaTeX Warning: Reference) .*undefined|Label\(s\) may have changed|Rerun/i;

export function cleanLog(log: string): string {
  return log.trim();
}

export function hasDocumentClass(source: string): boolean {
  const uncommented = source.replace(/(?<!\\)%.*/g, "");
  return /\\documentclass\s*(?:\[[^\]]*\])?\s*\{[^}]+\}/.test(uncommented);
}

// ---------------------------------------------------------------------------
// Compile via API

export async function compileLatex(
  files: LatexFileInput[],
  entryPath: string,
  options: {
    engine?: LatexEngine;
    signal?: AbortSignal;
  } = {},
): Promise<LatexOutcome> {
  const { engine = "pdftex", signal } = options;
  if (signal?.aborted) return { status: "cancelled" };

  const source = files.find((f) => f.path === entryPath)?.content;
  if (source === undefined) {
    return {
      status: "failed",
      message: `Entry file "${entryPath}" is not in the project.`,
      log: "",
    };
  }
  if (!entryPath.toLowerCase().endsWith(".tex")) {
    return {
      status: "failed",
      message: `"${entryPath}" is not a .tex file, so there is nothing to typeset.`,
      log: "",
    };
  }
  if (!hasDocumentClass(source)) {
    return {
      status: "failed",
      message: `${entryPath} has no \\documentclass, so TeX has nothing to typeset. Start it with something like \\documentclass{article}.`,
      log: "",
    };
  }

  try {
    const resp = await api.compileLatex(entryPath, files, engine, signal);

    if (resp.status === "ok" && resp.pdf_base64) {
      const binary = atob(resp.pdf_base64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      return { status: "ok", pdf: bytes, log: resp.log };
    }

    return {
      status: "failed",
      message: resp.message,
      log: resp.log,
    };
  } catch (caught) {
    if (signal?.aborted) return { status: "cancelled" };
    const msg = caught instanceof Error ? caught.message : String(caught);
    if (msg.includes("503")) {
      return {
        status: "failed",
        message:
          "The LaTeX compile service is not available. Run `make latex-image` and restart the API.",
        log: "",
      };
    }
    return { status: "failed", message: msg, log: "" };
  }
}

// ---------------------------------------------------------------------------
// The view

export type LatexPreviewProps = {
  files: LatexFileInput[];
  entryPath: string;
  engine?: LatexEngine;
  debounceMs?: number;
  downloadName?: string;
  className?: string;
};

function pdfFileName(entryPath: string, downloadName?: string): string {
  if (downloadName) return downloadName.endsWith(".pdf") ? downloadName : `${downloadName}.pdf`;
  const base = entryPath.slice(entryPath.lastIndexOf("/") + 1);
  return `${base.replace(/\.tex$/i, "") || "document"}.pdf`;
}

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
  const [phase, setPhase] = useState<"idle" | "compiling">("idle");
  const [showLog, setShowLog] = useState(false);
  const [nonce, setNonce] = useState(0);
  const run = useRef(0);
  const urlRef = useRef("");

  const signature = useMemo(
    () => JSON.stringify([entryPath, engine, files.map((file) => [file.path, file.content])]),
    [entryPath, engine, files],
  );

  useEffect(() => {
    const generation = ++run.current;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setPhase("compiling");
      const outcome = await compileLatex(files, entryPath, {
        engine,
        signal: controller.signal,
      });
      if (run.current !== generation) return;
      if (outcome.status === "cancelled") return;
      setPhase("idle");
      if (outcome.status === "ok") {
        // Copied into a fresh view rather than passed straight through: the
        // engine types its output as Uint8Array<ArrayBufferLike>, and that union
        // includes SharedArrayBuffer, which BlobPart will not accept. The copy is
        // a few hundred KB and buys a type that is actually true.
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
    }, debounceMs);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [signature, debounceMs, nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(
    () => () => {
      run.current += 1;
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    },
    [],
  );

  const recompile = useCallback(() => setNonce((value) => value + 1), []);

  const busy = phase !== "idle";
  const status = busy
    ? "Compiling…"
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
      {showLog && log && <pre className="project-preview-error">{log}</pre>}

      {pdfUrl ? (
        <iframe className="project-preview-frame" title="PDF preview" src={pdfUrl} />
      ) : (
        <div className="project-preview-empty">
          {error ? "Fix the error above to see the PDF." : "Compiling the document…"}
        </div>
      )}
    </div>
  );
}
