import { defineConfig } from "vitest/config";

export default defineConfig({
  // tsconfig says jsx: "preserve" for Next to handle, which leaves esbuild on
  // the classic transform and a rendered component reaching for a global React.
  // Next itself compiles with the automatic runtime, so match it here.
  esbuild: { jsx: "automatic" },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["tests/**/*.test.ts"],
  },
});

