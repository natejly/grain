import { describe, expect, it } from "vitest";
import {
  SANDBOX_CSP,
  htmlDocument,
  isHtmlEntry,
  isShimSpecifier,
  loaderFor,
  resolveVirtualImport,
  sandboxDocument,
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

describe("html entry points", () => {
  const files = [
    { path: "app.js", content: "document.title = 'ran';" },
    { path: "styles.css", content: "body { color: rebeccapurple }" },
    {
      path: "index.html",
      content:
        '<!doctype html><html><head><title>Hand written</title>' +
        '<link rel="stylesheet" href="./styles.css"></head>' +
        '<body><h1>Hello</h1><script src="./app.js"></script></body></html>',
    },
    { path: "fragment.html", content: "<p>No wrapper at all</p>" },
  ];

  it("recognises .html and .htm entry points and nothing else", () => {
    expect(isHtmlEntry("index.html")).toBe(true);
    expect(isHtmlEntry("PAGE.HTM")).toBe(true);
    expect(isHtmlEntry("index.tsx")).toBe(false);
    expect(isHtmlEntry("notes.html.txt")).toBe(false);
  });

  it("runs under the same locked policy as a compiled bundle", () => {
    const compiled = sandboxDocument({ js: "", css: "" }, "");
    const hand = htmlDocument(files, "index.html");
    // Byte-identical policy, not merely a similar one: the whole argument for
    // rendering hand-written HTML through this frame is that it is the SAME
    // frame, so a drift between the two would be the boundary quietly relaxing.
    expect(hand).toContain(SANDBOX_CSP);
    expect(compiled).toContain(SANDBOX_CSP);
    expect(SANDBOX_CSP).toContain("default-src 'none'");
    expect(SANDBOX_CSP).toContain("connect-src 'none'");
  });

  it("puts the policy ahead of any script the author wrote", () => {
    const hand = htmlDocument(files, "index.html");
    // A meta CSP only governs what follows it. An implementation that appended
    // the policy to the author's own <head> would pass a "contains the CSP"
    // assertion while governing nothing that ran before it.
    expect(hand.indexOf("Content-Security-Policy")).toBeLessThan(
      hand.indexOf("<script>"),
    );
    expect(hand.indexOf("Content-Security-Policy")).toBeLessThan(
      hand.indexOf("Hand written"),
    );
  });

  it("inlines the project's own scripts and stylesheets", () => {
    const hand = htmlDocument(files, "index.html");
    // The frame has no network, so a src= reference would silently do nothing.
    expect(hand).toContain("document.title = 'ran';");
    expect(hand).toContain("body { color: rebeccapurple }");
    expect(hand).not.toContain('src="./app.js"');
    expect(hand).not.toContain('href="./styles.css"');
    // And the author's own markup survives the rebuild.
    expect(hand).toContain("<h1>Hello</h1>");
    expect(hand).toContain("<title>Hand written</title>");
  });

  it("leaves references it cannot resolve alone rather than inventing a network", () => {
    const remote = [
      ...files,
      {
        path: "remote.html",
        content:
          '<html><body><script src="https://cdn.example.com/x.js"></script>' +
          '<script src="../../escape.js"></script></body></html>',
      },
    ];
    const hand = htmlDocument(remote, "remote.html");
    expect(hand).toContain("https://cdn.example.com/x.js");
    // A path walking above the project root resolves to nothing, exactly as it
    // does for an import — it must never reach a neighbouring file.
    expect(hand).toContain("../../escape.js");
    expect(hand).not.toContain("document.title = 'ran';");
  });

  it("treats a bare fragment as a body", () => {
    const hand = htmlDocument(files, "fragment.html");
    expect(hand).toContain("<p>No wrapper at all</p>");
    expect(hand).toContain(SANDBOX_CSP);
  });

  it("refuses an entry that is not in the project", () => {
    expect(() => htmlDocument(files, "missing.html")).toThrow(/not in the project/);
  });
});
