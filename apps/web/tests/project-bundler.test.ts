import { describe, expect, it } from "vitest";
import {
  isShimSpecifier,
  loaderFor,
  resolveVirtualImport,
} from "../components/project-bundler";

describe("sandbox shim lookup", () => {
  it("matches only the specifiers the sandbox ships", () => {
    for (const name of ["react", "react-dom", "react-dom/client", "react/jsx-runtime"]) {
      expect(isShimSpecifier(name)).toBe(true);
    }
    expect(isShimSpecifier("lodash")).toBe(false);
  });

  it("does not answer to Object.prototype keys", () => {
    // A plain-object lookup table says yes to all of these and then hands
    // esbuild a function as module contents, failing the build with an
    // internal plugin error instead of "the sandbox has no package install".
    for (const name of ["toString", "constructor", "__proto__", "valueOf", "hasOwnProperty"]) {
      expect(isShimSpecifier(name)).toBe(false);
    }
  });
});

describe("loaderFor", () => {
  it("falls back to text for unknown and inherited extensions", () => {
    expect(loaderFor("src/App.tsx")).toBe("tsx");
    expect(loaderFor("styles.css")).toBe("css");
    expect(loaderFor("README")).toBe("text");
    expect(loaderFor("weird.constructor")).toBe("text");
    expect(loaderFor("weird.toString")).toBe("text");
  });
});

describe("resolveVirtualImport", () => {
  const files = new Map([
    ["index.tsx", ""],
    ["App.tsx", ""],
    ["src/util.ts", ""],
    ["src/parts/index.ts", ""],
  ]);

  it("resolves relative imports and extensionless directories", () => {
    expect(resolveVirtualImport("./App", "index.tsx", files)).toBe("App.tsx");
    expect(resolveVirtualImport("./util", "src/parts/index.ts", files)).toBe(null);
    expect(resolveVirtualImport("../util", "src/parts/index.ts", files)).toBe("src/util.ts");
    expect(resolveVirtualImport("./parts", "src/util.ts", files)).toBe("src/parts/index.ts");
  });

  it("refuses to walk above the project root", () => {
    expect(resolveVirtualImport("../App", "index.tsx", files)).toBe(null);
    expect(resolveVirtualImport("../../App", "src/util.ts", files)).toBe(null);
    expect(resolveVirtualImport("../../../etc/passwd", "src/parts/index.ts", files)).toBe(null);
    expect(resolveVirtualImport("/../App", "index.tsx", files)).toBe(null);
  });
});
