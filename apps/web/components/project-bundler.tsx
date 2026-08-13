"use client";

import { AlertTriangle, RefreshCw } from "lucide-react";
import type { OutputFile, Plugin } from "esbuild-wasm";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export type ProjectFileInput = { path: string; content: string };

export type BundleResult = { js: string; css: string };

/** Generated into public/sandbox by scripts/sync-sandbox-assets.mjs. */
const WASM_URL = "/sandbox/esbuild.wasm";
const REACT_RUNTIME_URL = "/sandbox/react-runtime.js";

// A Map, not an object literal: `LOADERS["constructor"]` on a plain object
// answers with something inherited from Object.prototype, and a file extension
// is attacker-shaped input.
const LOADERS = new Map<string, "ts" | "tsx" | "js" | "jsx" | "css" | "json" | "text">([
  ["ts", "ts"],
  ["tsx", "tsx"],
  ["js", "js"],
  ["mjs", "js"],
  ["jsx", "jsx"],
  ["css", "css"],
  ["json", "json"],
]);

// Tried in order when an import omits the extension, the way a bundler with a
// real filesystem would stat them.
const EXTENSIONS = [".tsx", ".ts", ".jsx", ".js", ".mjs", ".json", ".css"];

const PROJECT_NAMESPACE = "project";
const SHIM_NAMESPACE = "sandbox-runtime";

/**
 * The only bare specifiers the sandbox can satisfy. Each shim is CommonJS on
 * purpose: esbuild then wires `import React from "react"` and
 * `import { useState } from "react"` through its own interop helpers, so both
 * import styles hit the same object the runtime bundle put on the global.
 *
 * Held in a Map so that only these five specifiers match. A plain object would
 * also answer to every Object.prototype key, so `import "toString"` would be
 * routed here and then loaded with a function as its contents, which fails the
 * build with an internal plugin error instead of the "no package install"
 * message the user needs.
 */
const SHIMS = new Map<string, string>(
  [
    "react",
    "react-dom",
    "react-dom/client",
    "react/jsx-runtime",
    "react/jsx-dev-runtime",
  ].map((name) => [
    name,
    `module.exports = globalThis.__PROJECT_REACT__[${JSON.stringify(name)}];`,
  ]),
);

/** True only for the bare specifiers the sandbox actually ships a shim for. */
export function isShimSpecifier(specifier: string): boolean {
  return SHIMS.has(specifier);
}

export function loaderFor(path: string) {
  const extension = path.slice(path.lastIndexOf(".") + 1).toLowerCase();
  return LOADERS.get(extension) ?? "text";
}

/**
 * Resolve `specifier` against `importer` inside the virtual project. Returns
 * null when nothing matches; `..` that walks past the root resolves to nothing
 * rather than to some neighbouring file.
 */
export function resolveVirtualImport(
  specifier: string,
  importer: string,
  files: Map<string, string>,
): string | null {
  const base = importer.includes("/") ? importer.slice(0, importer.lastIndexOf("/")) : "";
  const absolute = specifier.startsWith("/");
  const parts = (absolute ? specifier.slice(1) : `${base}/${specifier}`).split("/");
  const stack: string[] = [];
  for (const part of parts) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (stack.length === 0) return null;
      stack.pop();
      continue;
    }
    stack.push(part);
  }
  const candidate = stack.join("/");
  if (!candidate) return null;
  const attempts = [
    candidate,
    ...EXTENSIONS.map((extension) => candidate + extension),
    ...EXTENSIONS.map((extension) => `${candidate}/index${extension}`),
  ];
  return attempts.find((attempt) => files.has(attempt)) ?? null;
}

function virtualFilesystem(files: Map<string, string>, entry: string): Plugin {
  return {
    name: "project-files",
    setup(build) {
      build.onResolve({ filter: /.*/ }, (args) => {
        if (args.kind === "entry-point") {
          return { path: entry, namespace: PROJECT_NAMESPACE };
        }
        if (isShimSpecifier(args.path)) {
          return { path: args.path, namespace: SHIM_NAMESPACE };
        }
        const resolved = resolveVirtualImport(args.path, args.importer, files);
        if (resolved) return { path: resolved, namespace: PROJECT_NAMESPACE };
        const bare = !args.path.startsWith(".") && !args.path.startsWith("/");
        return {
          errors: [
            {
              text: bare
                ? `Cannot import "${args.path}": the sandbox has no package install. ` +
                  `Only "react", "react-dom" and paths inside the project resolve.`
                : `Cannot find "${args.path}" imported from "${args.importer}"`,
            },
          ],
        };
      });

      build.onLoad({ filter: /.*/, namespace: PROJECT_NAMESPACE }, (args) => ({
        contents: files.get(args.path) ?? "",
        loader: loaderFor(args.path),
      }));

      build.onLoad({ filter: /.*/, namespace: SHIM_NAMESPACE }, (args) => ({
        contents: SHIMS.get(args.path) ?? "",
        loader: "js" as const,
      }));
    },
  };
}

let esbuildPromise: Promise<typeof import("esbuild-wasm")> | null = null;

function loadEsbuild(): Promise<typeof import("esbuild-wasm")> {
  if (!esbuildPromise) {
    esbuildPromise = import("esbuild-wasm")
      .then(async (esbuild) => {
        try {
          // worker: false keeps the compiler off a blob: worker, which the host
          // page's CSP does not allow. Builds are small enough to stay snappy.
          await esbuild.initialize({ wasmURL: WASM_URL, worker: false });
        } catch (caught) {
          // A hot reload can re-run this against an already-initialized module.
          if (!String(caught).includes("more than once")) throw caught;
        }
        return esbuild;
      })
      .catch((caught) => {
        esbuildPromise = null;
        throw caught;
      });
  }
  return esbuildPromise;
}

let reactRuntimePromise: Promise<string> | null = null;

function loadReactRuntime(): Promise<string> {
  if (!reactRuntimePromise) {
    reactRuntimePromise = fetch(REACT_RUNTIME_URL)
      .then((response) => {
        if (!response.ok) throw new Error(`React runtime asset is missing (${response.status})`);
        return response.text();
      })
      .catch((caught) => {
        reactRuntimePromise = null;
        throw caught;
      });
  }
  return reactRuntimePromise;
}

function formatFailure(caught: unknown): string {
  const errors = (caught as { errors?: { text: string; location?: { file?: string; line?: number; column?: number; lineText?: string } }[] })
    .errors;
  if (!errors?.length) {
    return caught instanceof Error ? caught.message : String(caught);
  }
  return errors
    .map((error) => {
      const where = error.location
        ? `${error.location.file}:${error.location.line}:${error.location.column}\n  ${error.location.lineText?.trim() ?? ""}\n`
        : "";
      return `${where}${error.text}`;
    })
    .join("\n\n");
}

/** Compile the project's files into one script plus its collected CSS. */
export async function bundleProject(
  files: ProjectFileInput[],
  entryPath: string,
): Promise<BundleResult> {
  const map = new Map(files.map((file) => [file.path, file.content]));
  if (!map.has(entryPath)) {
    throw new Error(`Entry file "${entryPath}" is not in the project`);
  }
  const esbuild = await loadEsbuild();
  const result = await esbuild.build({
    entryPoints: ["<entry>"],
    bundle: true,
    write: false,
    format: "iife",
    platform: "browser",
    target: "es2020",
    // Automatic JSX so files need no React import; the classic style still
    // compiles because an explicit React import is simply left in place.
    jsx: "automatic",
    define: { "process.env.NODE_ENV": '"production"' },
    logLevel: "silent",
    outdir: "/out",
    plugins: [virtualFilesystem(map, entryPath)],
  });
  const outputs: OutputFile[] = result.outputFiles ?? [];
  return {
    js: outputs.filter((file) => file.path.endsWith(".js")).map((file) => file.text).join("\n"),
    css: outputs.filter((file) => file.path.endsWith(".css")).map((file) => file.text).join("\n"),
  };
}

/**
 * Inline scripts end at the first `</script`, wherever it sits in the source.
 * Escaping the slash keeps the byte sequence out of the HTML tokenizer while
 * leaving the JavaScript equivalent.
 */
function inlineSafe(code: string): string {
  return code.replace(/<\/(script|style)/gi, "<\\/$1").replace(/<!--/g, "<\\!--");
}

/**
 * The one policy every previewed document runs under (ADR 0004): no network of
 * any kind, inline script and style only, images and fonts from data: URIs.
 *
 * Named once and shared by both document builders below. A hand-written HTML
 * file is no more trustworthy than a generated bundle — it is the same
 * workspace, the same agent writing into it, and often the same prompt
 * injection — so the two must not be able to drift into different policies.
 */
export const SANDBOX_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'";

const BASE_STYLE =
  "html,body{margin:0;background:#fff;color:#111;font:14px/1.5 system-ui,sans-serif}";

/**
 * The document handed to the iframe. Its CSP is `default-src 'none'` — no
 * network of any kind — so React and the compiled bundle are inlined rather
 * than linked, and the frame is sandboxed to scripts only (opaque origin, no
 * cookies, no reach into this page).
 */
export function sandboxDocument(bundle: BundleResult, runtime: string): string {
  return `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="${SANDBOX_CSP}">
<style>${BASE_STYLE}</style>
<style>${inlineSafe(bundle.css)}</style>
</head>
<body>
<div id="root"></div>
<script>${inlineSafe(runtime)}</script>
<script>${inlineSafe(bundle.js)}</script>
</body>
</html>`;
}

/** True for an entry point that is HTML rather than something to compile. */
export function isHtmlEntry(path: string): boolean {
  const lower = path.toLowerCase();
  return lower.endsWith(".html") || lower.endsWith(".htm");
}

function section(source: string, tag: "head" | "body"): string | null {
  const open = new RegExp(`<${tag}\\b[^>]*>`, "i").exec(source);
  if (!open) return null;
  const start = open.index + open[0].length;
  const close = new RegExp(`</${tag}\\s*>`, "i").exec(source.slice(start));
  return close ? source.slice(start, start + close.index) : source.slice(start);
}

/**
 * Inline the project's own scripts and stylesheets into the markup.
 *
 * The frame has no network — `default-src 'none'` — so a `<script src="app.js">`
 * would silently do nothing, and "my script did not run" with no error is the
 * worst possible first experience of a hand-written page. Inlining resolves the
 * reference through the SAME virtual filesystem the bundler uses, so `./`, `../`
 * and extensionless paths behave as they do in an import, and a path that walks
 * above the project root resolves to nothing rather than to a neighbour.
 *
 * References that do not resolve inside the project are left exactly as written
 * and the CSP refuses them. That is deliberate: rewriting a CDN URL into
 * something that "works" would be inventing a network the sandbox does not have.
 */
function inlineLocalAssets(source: string, files: Map<string, string>, entry: string): string {
  const withScripts = source.replace(
    /<script\b([^>]*)\bsrc\s*=\s*["']([^"']+)["']([^>]*)>\s*<\/script\s*>/gi,
    (whole, _before, href: string) => {
      const resolved = resolveVirtualImport(href, entry, files);
      if (resolved === null) return whole;
      return `<script>${inlineSafe(files.get(resolved) ?? "")}</script>`;
    },
  );
  return withScripts.replace(/<link\b[^>]*>/gi, (whole) => {
    if (!/rel\s*=\s*["']?stylesheet/i.test(whole)) return whole;
    const href = /href\s*=\s*["']([^"']+)["']/i.exec(whole);
    if (!href) return whole;
    const resolved = resolveVirtualImport(href[1], entry, files);
    if (resolved === null) return whole;
    return `<style>${inlineSafe(files.get(resolved) ?? "")}</style>`;
  });
}

/**
 * An `.html` entry point, wrapped for the same locked frame the bundler's
 * output goes into.
 *
 * The page is REBUILT around our own `<head>` rather than having a meta tag
 * spliced into the author's, and that is the security-relevant part: a
 * `<meta http-equiv="Content-Security-Policy">` only governs what follows it, so
 * a policy appended after the author's first inline `<script>` would arrive too
 * late to govern the one thing worth governing. Ours is the first child of the
 * head, always, whatever the file looked like.
 *
 * A file with no `<html>`/`<body>` wrapper is treated as a body fragment, which
 * is what a browser does with it too.
 */
export function htmlDocument(
  files: ProjectFileInput[],
  entryPath: string,
): string {
  const map = new Map(files.map((file) => [file.path, file.content]));
  const source = map.get(entryPath);
  if (source === undefined) {
    throw new Error(`Entry file "${entryPath}" is not in the project`);
  }
  const inlined = inlineLocalAssets(source, map, entryPath);
  const head = section(inlined, "head");
  const body = section(inlined, "body");
  return `<!doctype html>
<html>
<head>
<meta http-equiv="Content-Security-Policy" content="${SANDBOX_CSP}">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>${BASE_STYLE}</style>
${head ?? ""}
</head>
<body>
${body ?? (head === null ? inlined : "")}
</body>
</html>`;
}

export type ProjectPreviewProps = {
  files: ProjectFileInput[];
  entryPath: string;
  /** Milliseconds of quiet typing before a rebuild. */
  debounceMs?: number;
  className?: string;
};

/**
 * Compiles the project in the browser and renders it in a locked-down frame.
 * A failed build never replaces a working preview: the last good document stays
 * on screen underneath the error, which is how a dev server behaves.
 */
export function ProjectPreview({
  files,
  entryPath,
  debounceMs = 400,
  className,
}: ProjectPreviewProps) {
  const [html, setHtml] = useState("");
  const [error, setError] = useState("");
  const [building, setBuilding] = useState(false);
  const [nonce, setNonce] = useState(0);
  const build = useRef(0);

  // Rebuilds key off content, not array identity, so a refetch that returns the
  // same files does not recompile.
  const signature = useMemo(
    () =>
      JSON.stringify([entryPath, files.map((file) => [file.path, file.content])]),
    [entryPath, files],
  );

  useEffect(() => {
    const generation = ++build.current;
    const timer = window.setTimeout(async () => {
      setBuilding(true);
      try {
        // An HTML entry point has nothing to compile: no esbuild, no React
        // runtime, no bundle. What it does share is the frame it lands in —
        // same CSP, same `sandbox="allow-scripts"` — because a hand-written page
        // is no more trustworthy than a generated one, and that boundary is the
        // whole of what ADR 0004 bought.
        const next = isHtmlEntry(entryPath)
          ? htmlDocument(files, entryPath)
          : await (async () => {
              const [bundle, runtime] = await Promise.all([
                bundleProject(files, entryPath),
                loadReactRuntime(),
              ]);
              return sandboxDocument(bundle, runtime);
            })();
        if (build.current !== generation) return;
        setHtml(next);
        setError("");
      } catch (caught) {
        if (build.current !== generation) return;
        setError(formatFailure(caught));
      } finally {
        if (build.current === generation) setBuilding(false);
      }
    }, debounceMs);
    return () => window.clearTimeout(timer);
    // `files` and `entryPath` are covered by `signature`; depending on them too
    // would rebuild on every parent render.
  }, [signature, debounceMs, nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  const rerun = useCallback(() => setNonce((value) => value + 1), []);

  return (
    <div className={className || "project-preview"}>
      <div className="project-preview-bar">
        <span className="project-preview-status">
          {building ? "Building…" : error ? "Build failed" : "Live preview"}
        </span>
        <button className="ghost-button" onClick={rerun} disabled={building}>
          <RefreshCw size={13} /> Rebuild
        </button>
      </div>
      {error && (
        <pre className="project-preview-error">
          <AlertTriangle size={13} /> {error}
        </pre>
      )}
      {html ? (
        <iframe
          className="project-preview-frame"
          title="Project preview"
          srcDoc={html}
          sandbox="allow-scripts"
        />
      ) : (
        <div className="project-preview-empty">
          {error ? "Fix the error above to see the preview." : "Compiling the project…"}
        </div>
      )}
    </div>
  );
}
