import { cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  LatexPreview,
  cleanLog,
  compileLatex,
  hasDocumentClass,
} from "../components/latex-compiler";

afterEach(cleanup);

// Mock the API client so compile tests don't need a running server.
vi.mock("../components/api", () => ({
  api: {
    compileLatex: vi.fn().mockResolvedValue({
      status: "failed",
      message: "mock: no server",
      log: "",
      pdf_base64: null,
    }),
  },
}));

describe("cleanLog", () => {
  it("trims whitespace from log output", () => {
    const log = "  This is pdfTeX, Version 3.141592653\nOutput written on main.pdf (1 page).  ";
    expect(cleanLog(log)).toBe(
      "This is pdfTeX, Version 3.141592653\nOutput written on main.pdf (1 page).",
    );
  });
});

describe("hasDocumentClass", () => {
  it("ignores a commented-out declaration", () => {
    expect(hasDocumentClass("\\documentclass[11pt]{article}")).toBe(true);
    expect(hasDocumentClass("% \\documentclass{article}\n\\section{x}")).toBe(false);
    expect(hasDocumentClass("50\\% off\n\\documentclass{article}")).toBe(true);
  });
});

describe("compileLatex pre-flight", () => {
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
  it("settles as cancelled without touching the engine when already aborted", async () => {
    const controller = new AbortController();
    controller.abort();
    const outcome = await compileLatex([], "nope.bib", { signal: controller.signal });
    expect(outcome).toEqual({ status: "cancelled" });
  });

  it("still runs normally when the signal is never aborted", async () => {
    const controller = new AbortController();
    const outcome = await compileLatex([], "main.tex", { signal: controller.signal });
    expect(outcome.status).toBe("failed");
    expect(outcome.status === "failed" && outcome.message).toMatch(/is not in the project/);
  });
});

describe("the CSP the PDF frame has to live under", () => {
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
    expect(csp).toContain("object-src 'none'");
  });
});

describe("LatexPreview", () => {
  it("mounts without touching the engine, and offers no PDF until there is one", () => {
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
