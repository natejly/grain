import { cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it } from "vitest";
import {
  LatexPreview,
  cleanLog,
  compileLatex,
  describeEngineFailure,
  hasDocumentClass,
  missingPackage,
  visibleDiagnostics,
} from "../components/latex-compiler";

afterEach(cleanup);

describe("cleanLog", () => {
  it("drops the runtime chatter that ends every transcript, success or not", () => {
    const log = [
      "This is pdfTeX, Version 3.141592653",
      "Output written on main.pdf (1 page).",
      "program exited (with status: 0), but keepRuntimeAlive() is set (counter=0)",
    ].join("\n");
    expect(cleanLog(log)).toBe(
      "This is pdfTeX, Version 3.141592653\nOutput written on main.pdf (1 page).",
    );
  });
});

describe("visibleDiagnostics", () => {
  // A three-pass run that ends in a perfect PDF still carries pass one's
  // complaints, because `log` is every pass concatenated.
  const rerunNoise = [
    { severity: "warning", message: "Citation `knuth1984' undefined" },
    { severity: "warning", message: "Reference `sec:intro' undefined" },
    { severity: "warning", message: "There were undefined references." },
    { severity: "warning", message: "Label(s) may have changed. Rerun to get them right." },
  ] as const;

  it("hides warnings the reruns already resolved when the compile succeeded", () => {
    expect(visibleDiagnostics(rerunNoise, true)).toEqual([]);
  });

  it("keeps them when the compile failed, where they are evidence", () => {
    expect(visibleDiagnostics(rerunNoise, false)).toHaveLength(4);
  });

  it("hides warnings from the TeX distribution's own files on success", () => {
    // The starter's graphicx drags this one in on every clean compile.
    const distribution = [
      {
        severity: "warning",
        message: "Package epstopdf Warning: Shell escape feature is not enabled.",
        file: "/texlive/texmf-dist/tex/latex/epstopdf-pkg/epstopdf-base.sty",
      },
    ] as const;
    expect(visibleDiagnostics(distribution, true)).toEqual([]);
    expect(visibleDiagnostics(distribution, false)).toHaveLength(1);
  });

  it("keeps real warnings and errors, de-duplicated across passes", () => {
    const diagnostics = [
      { severity: "warning", message: "Overfull \\hbox (12pt too wide)", file: "main.tex", line: 9 },
      { severity: "warning", message: "Overfull \\hbox (12pt too wide)", file: "main.tex", line: 9 },
      { severity: "error", message: "Undefined control sequence.", file: "main.tex", line: 3 },
    ] as const;
    expect(visibleDiagnostics(diagnostics, true)).toHaveLength(2);
  });
});

describe("missingPackage", () => {
  it("names the package behind TeX's file-not-found", () => {
    expect(
      missingPackage([
        { severity: "error", message: "LaTeX Error: File `tikz.sty' not found.", line: 1 },
      ]),
    ).toBe("tikz");
    expect(
      missingPackage([{ severity: "error", message: "LaTeX Error: File `beamer.cls' not found." }]),
    ).toBe("beamer");
    expect(
      missingPackage([{ severity: "error", message: "Undefined control sequence." }]),
    ).toBeNull();
  });
});

describe("hasDocumentClass", () => {
  it("ignores a commented-out declaration", () => {
    expect(hasDocumentClass("\\documentclass[11pt]{article}")).toBe(true);
    expect(hasDocumentClass("% \\documentclass{article}\n\\section{x}")).toBe(false);
    expect(hasDocumentClass("50\\% off\n\\documentclass{article}")).toBe(true);
  });
});

describe("describeEngineFailure", () => {
  it("turns each engine error class into something actionable", () => {
    expect(describeEngineFailure({ name: "WorkerCrashedError" })).toMatch(/crashed/i);
    expect(
      describeEngineFailure({ name: "FatalError", code: "unsupported-engine" }),
    ).toMatch(/pdfTeX or XeTeX/);
    expect(
      describeEngineFailure({
        name: "FatalError",
        code: "init-failed",
        message: "failed to load an asset manifest (/latex/assets/manifest.json: HTTP 404)",
      }),
    ).toMatch(/sandbox-assets/);
    expect(describeEngineFailure({ name: "AssetVersionMismatchError" })).toMatch(
      /different versions/,
    );
    expect(describeEngineFailure(new Error("boom"))).toBe("boom");
  });
});

describe("compileLatex pre-flight", () => {
  // These all return before the 79 MiB engine is touched, which is the point:
  // a document that cannot typeset should not cost a download.
  it("reports a missing entry file", async () => {
    const outcome = await compileLatex([{ path: "a.tex", content: "" }], "main.tex");
    expect(outcome).toMatchObject({ status: "failed" });
    expect(outcome.status === "failed" && outcome.message).toMatch(/is not in the project/);
  });

  it("refuses an entry that is not a .tex file", async () => {
    const outcome = await compileLatex([{ path: "refs.bib", content: "" }], "refs.bib");
    expect(outcome.status === "failed" && outcome.message).toMatch(/not a \.tex file/);
  });

  it("explains a missing \\documentclass instead of letting TeX misdiagnose it", async () => {
    const outcome = await compileLatex(
      [{ path: "main.tex", content: "\\section{Intro}\nHello.\n" }],
      "main.tex",
    );
    expect(outcome.status === "failed" && outcome.message).toMatch(/no \\documentclass/);
  });
});

describe("superseding a compile", () => {
  // The engine runs one job at a time and QUEUES the rest. A caller that starts
  // a new compile without retiring the old one therefore does not replace it —
  // it lines up behind it. Measured in Chromium against the real engine: with a
  // runaway document compiling, a fix typed 3 s later took 28,370 ms to appear
  // (it waited out the stale job's whole 30 s timeout); with the abort wired it
  // took 828 ms. Worse, the queued job's own timeout runs while it waits, so a
  // one-line valid document could fail with "A runaway macro or an unterminated
  // environment is the usual cause".
  it("settles as cancelled without touching the engine when already aborted", async () => {
    const controller = new AbortController();
    controller.abort();
    // A bogus entry path proves the abort short-circuits ahead of every check:
    // an abandoned run has no error to report, only that nobody wants it.
    const outcome = await compileLatex([], "nope.bib", { signal: controller.signal });
    expect(outcome).toEqual({ status: "cancelled" });
  });

  it("still runs normally when the signal is never aborted", async () => {
    const controller = new AbortController();
    const outcome = await compileLatex([], "main.tex", { signal: controller.signal });
    // Reaches the ordinary pre-flight rather than being swallowed as cancelled.
    expect(outcome.status).toBe("failed");
    expect(outcome.status === "failed" && outcome.message).toMatch(/is not in the project/);
  });
});

describe("the CSP the PDF frame has to live under", () => {
  // LatexPreview shows the compiled PDF in an <iframe src="blob:…">. A blob:
  // URL is framing, so it is checked against frame-src — not default-src — and
  // a frame-src that lists only the API origins blocks it. The compile still
  // succeeds and the status still reads "Compiled", so the failure is silent:
  // the user gets an editor that types fine and never shows a document.
  it("lets the compiled PDF be framed from its blob: URL", async () => {
    const { default: config } = await import("../next.config");
    const headers = await config.headers!();
    const csp = headers
      .flatMap((entry) => entry.headers)
      .find((header) => header.key === "Content-Security-Policy")!.value;
    const frameSrc = csp
      .split(";")
      .map((directive) => directive.trim())
      .find((directive) => directive.startsWith("frame-src"))!;
    expect(frameSrc).toBeDefined();
    expect(frameSrc.split(/\s+/)).toContain("blob:");
    // object-src stays 'none': <embed>/<object> are not how this renders.
    expect(csp).toContain("object-src 'none'");
  });
});

describe("LatexPreview", () => {
  it("mounts without touching the engine, and offers no PDF until there is one", () => {
    // Unmounted well inside the debounce window, so nothing is ever compiled —
    // which is the guarantee that matters: opening the view is not a download.
    const { unmount } = render(
      createElement(LatexPreview, {
        files: [{ path: "main.tex", content: "\\documentclass{article}" }],
        entryPath: "main.tex",
      }),
    );
    expect(screen.getByText("Nothing compiled yet")).toBeTruthy();
    expect(screen.getByText("Recompile")).toBeTruthy();
    expect(screen.queryByText("PDF")).toBeNull();
    unmount();
  });
});
