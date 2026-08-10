import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({ baseDirectory: import.meta.dirname });
const config = [
  {
    ignores: [
      ".next/**",
      ".next-e2e/**",
      "next-env.d.ts",
      "node_modules/**",
      // Generated from node_modules by scripts/sync-sandbox-assets.mjs — a
      // prebuilt React bundle for the sandbox frame, not our source.
      "public/sandbox/**",
      // Same script: the vendored TeX Live WebAssembly build and its worker.
      "public/latex/**",
    ],
  },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
];

export default config;
