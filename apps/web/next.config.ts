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
  "img-src 'self' data:",
  "font-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  `script-src 'self' 'unsafe-inline'${process.env.NODE_ENV === "development" ? " 'unsafe-eval'" : ""}`,
  `connect-src 'self' ${apiOrigins}`,
  `frame-src ${apiOrigins}`,
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
