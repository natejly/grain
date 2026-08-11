import type { NextConfig } from "next";

const apiOrigin = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
// localhost and 127.0.0.1 are distinct origins; allow both spellings so the
// browser side works regardless of which host the user opens.
const apiOrigins = Array.from(
  new Set([
    apiOrigin,
    apiOrigin.replace("//localhost", "//127.0.0.1"),
    apiOrigin.replace("//127.0.0.1", "//localhost"),
  ]),
).join(" ");
const contentSecurityPolicy = [
  "default-src 'self'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  "object-src 'none'",
  // `blob:` for the same reason `frame-src` has it, and it is the third thing
  // that had to be true before any chart the sandbox drew could be seen. A
  // stored figure lives behind an authenticated route on the API origin, so it
  // is fetched through the API client and handed to the <img> as a blob: URL
  // this document created — which `img-src 'self' data:` blocked outright, with
  // no error anywhere but the browser console. Adding the API origin here
  // instead would have been both wider and useless: a cross-site <img> carries
  // no workspace header and no third-party cookie in Safari. A blob: URL names
  // only bytes this document already holds and reaches no network.
  "img-src 'self' data: blob:",
  "font-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  // 'wasm-unsafe-eval' is required by esbuild-wasm, which bundles project files
  // in this page so the sandbox never needs a server or a network fetch. It
  // permits WebAssembly compilation only — not eval() or new Function() — and it
  // applies to the host page alone. The generated preview still runs in an
  // iframe under default-src 'none' with connect-src 'none', so the sandbox's
  // own guarantees are unchanged.
  `script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'${process.env.NODE_ENV === "development" ? " 'unsafe-eval'" : ""}`,
  `connect-src 'self' ${apiOrigins}`,
  // `blob:` is what the LaTeX preview frames: the compiled PDF never leaves the
  // browser, so it is handed to the <iframe> as a same-origin blob: URL this
  // page created itself. Without it the compile succeeds, the status reads
  // "Compiled", and the frame is silently blocked — a working editor that shows
  // nothing. It grants no network reach: a blob: URL can only name bytes this
  // document already holds.
  `frame-src 'self' blob: ${apiOrigins}`,
].join("; ");

const nextConfig: NextConfig = {
  transpilePackages: ["@workspace/api-client"],
  allowedDevOrigins: ["127.0.0.1"],
  distDir: process.env.NEXT_DIST_DIR || ".next",
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "Content-Security-Policy", value: contentSecurityPolicy },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=(), payment=()",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
