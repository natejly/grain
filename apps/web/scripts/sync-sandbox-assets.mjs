// Builds the local assets the browser needs to compile things without a server,
// into public/sandbox/ and public/latex/. All of it is derived from node_modules
// or from a pinned upstream archive, so it is generated rather than committed —
// run this before `next dev` / `next build`.
//
//   sandbox/esbuild.wasm      the compiler itself; loading it from a CDN would
//                             put a third party in the path of every rebuild
//   sandbox/react-runtime.js  React, inlined into the sandbox frame, which has
//                             no network
//   latex/worker.js           the TeX engine's worker, which must be same-origin
//   latex/assets/*            TeX Live as WebAssembly, ~79 MiB (see below)
import { build } from "esbuild-wasm";
import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { copyFile, mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const web = resolve(here, "..");
const out = resolve(web, "public/sandbox");

await mkdir(out, { recursive: true });
await copyFile(
  resolve(web, "node_modules/esbuild-wasm/esbuild.wasm"),
  resolve(out, "esbuild.wasm"),
);

await build({
  entryPoints: [resolve(here, "react-runtime-entry.mjs")],
  outfile: resolve(out, "react-runtime.js"),
  bundle: true,
  format: "iife",
  minify: true,
  // React reads this at module scope; without it the bundle pulls in the
  // development build and warns about a missing process.
  define: { "process.env.NODE_ENV": '"production"' },
});

console.log(`sandbox assets written to ${out}`);

// ---------------------------------------------------------------------------
// LaTeX engine
//
// Server-side TeX Live compilation has replaced the in-browser wasmtex engine.
// The 79 MiB local asset tree is no longer required for `pnpm dev` — LaTeX
// projects compile via POST /api/latex/compile against a Docker image built
// with `make latex-image`. The wasmtex sync below is kept as a no-op comment
// for reference but is no longer executed.

console.log("latex preview uses server-side TeX Live — no local assets needed");
