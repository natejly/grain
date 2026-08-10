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

const latexOut = resolve(web, "public/latex");
const latexAssetsOut = resolve(latexOut, "assets");

// The engine's own JS ships on npm; the TeX tree does not, by design — it is a
// 435 MB release archive of which we vendor only the "core" tier. These five are
// everything a compile touches: the formats/*.fmt files are listed in the
// manifest but are already inside core.data and are never fetched, and the
// 506 MB "academic" tier (tikz, beamer, biblatex…) is deliberately not shipped.
const VENDORED = ["manifest.json", "busytex.js", "busytex.wasm", "core.js", "core.data"];

// Where a previously fetched archive is unpacked. Under node_modules so it
// survives a `next build` and is already ignored by git.
const cache = resolve(web, "node_modules/.cache/wasmtex-assets");

const archiveUrl = (version) =>
  `https://github.com/bofeizhu/wasmtex/releases/download/assets-v${version}/` +
  `wasmtex-assets-${version}.tar.gz`;

async function sha256(path) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest("hex");
}

async function exists(path) {
  return stat(path).then(
    () => true,
    () => false,
  );
}

/** The asset version the installed wasmtex demands, straight from the package. */
async function expectedAssetsVersion() {
  const { ASSETS_VERSION } = await import("wasmtex");
  return ASSETS_VERSION;
}

/**
 * Copy one asset from the cache, verifying it against the manifest's hash first.
 * A truncated download is otherwise indistinguishable from a good one until the
 * engine fails to instantiate with an unreadable error, so this is cheap.
 */
async function vendor(name, entry) {
  const source = resolve(cache, name);
  const target = resolve(latexAssetsOut, name);
  const size = (await stat(source)).size;
  if (entry && entry.bytes !== size) {
    throw new Error(`${name} is ${size} bytes, manifest says ${entry.bytes} — re-download`);
  }
  if (entry?.sha256) {
    const digest = await sha256(source);
    if (digest !== entry.sha256) {
      throw new Error(`${name} sha256 ${digest} does not match the manifest — re-download`);
    }
  }
  await mkdir(dirname(target), { recursive: true });
  await copyFile(source, target);
  return size;
}

async function syncLatexAssets() {
  const version = await expectedAssetsVersion();
  const manifestPath = resolve(cache, "manifest.json");
  if (!(await exists(manifestPath))) {
    // Not fatal: a 79 MiB download has no business happening implicitly on
    // every `pnpm dev`, and everything except the LaTeX preview works without
    // it. The view itself says the same thing at runtime.
    console.warn(
      `latex assets are not present — the LaTeX preview will be unavailable.\n` +
        `  To install them (~435 MB downloaded, ~79 MiB kept):\n` +
        `    mkdir -p ${cache} && curl -L ${archiveUrl(version)} \\\n` +
        `      | tar xz -C ${cache} --exclude='*academic*'\n` +
        `    pnpm --filter @workspace/web sandbox-assets`,
    );
    return;
  }

  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  // wasmtex refuses to boot against a manifest whose version differs from the
  // package's, so catching the skew here turns a runtime FatalError in the
  // user's browser into a build-time message with the fix in it.
  if (manifest.version && manifest.version !== version) {
    throw new Error(
      `latex assets are version ${manifest.version} but wasmtex expects ${version}. ` +
        `Delete ${cache} and re-download from ${archiveUrl(version)}`,
    );
  }
  const entries = new Map((manifest.assets ?? []).map((entry) => [entry.path, entry]));

  await mkdir(latexAssetsOut, { recursive: true });
  let total = 0;
  for (const name of VENDORED) total += await vendor(name, entries.get(name));

  await copyFile(
    resolve(web, "node_modules/wasmtex/dist/worker.js"),
    resolve(latexOut, "worker.js"),
  );
  // This directory is generated and enormous; keep it out of git regardless of
  // what the repo's own ignore file says. `*` covers this file too, so nothing
  // here is ever tracked.
  await writeFile(resolve(latexOut, ".gitignore"), "*\n");

  console.log(
    `latex assets written to ${latexOut} (${(total / 1024 / 1024).toFixed(1)} MiB, ` +
      `assets v${version})`,
  );
}

await syncLatexAssets();
