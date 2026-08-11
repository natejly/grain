import { expect, test } from "@playwright/test";
import { createFromMenu } from "./shell";

declare global {
  interface Window {
    /** Set by this spec's init script; see the comment on the hook below. */
    __pdfBlobs?: { url: string; size: number; magic: string }[];
  }
}

/**
 * The gap that let "latex isn't rendering" ship twice: fifteen unit tests cover
 * the compiler's log parsing and none of them run TeX. Nothing anywhere proved
 * that picking LaTeX in the UI ends with a PDF, which is the only claim a user
 * cares about — so this drives the whole path, from the Create menu to the
 * bytes the preview frames.
 */
test("Create → LaTeX document compiles to a real PDF", async ({ page }) => {
  // The first compile boots ~79 MiB of TeX Live out of public/latex before TeX
  // itself starts, so this is nothing like the other specs' budgets.
  test.setTimeout(420_000);

  // Catch the PDF on its way into the preview.
  //
  // Reading it back from the frame's blob: URL is not an option and should not
  // be: the page's CSP lists blob: in frame-src only, so fetch() of that URL is
  // blocked — which is exactly the restriction that keeps compiled bytes inside
  // the browser. Wrapping createObjectURL sees the same Blob the frame gets,
  // needs nothing from the app, and reading a Blob is not a fetch.
  await page.addInitScript(() => {
    const create = URL.createObjectURL.bind(URL);
    window.__pdfBlobs = [];
    URL.createObjectURL = (object: Blob | MediaSource) => {
      const url = create(object);
      if (object instanceof Blob && object.type === "application/pdf") {
        void object.arrayBuffer().then((buffer) => {
          window.__pdfBlobs?.push({
            url,
            size: buffer.byteLength,
            magic: new TextDecoder().decode(buffer.slice(0, 5)),
          });
        });
      }
      return url;
    };
  });

  await page.goto("/");
  await createFromMenu(page, "LaTeX document", "E2E TeX Paper");

  // A *project*, not a document: the seeded main.tex and the Projects editor
  // are what make a PDF possible at all. The document format that used to be
  // called LaTeX renders KaTeX and compiles nothing, which is the confusion
  // this whole path was rewired to remove.
  await expect(page.getByRole("heading", { name: "E2E TeX Paper" })).toBeVisible();
  await expect(page.locator(".project-file-name", { hasText: "main.tex" })).toBeVisible();

  const preview = page.locator(".project-preview-pane");
  const status = preview.locator(".project-preview-status");
  // Settle on an outcome first. Waiting on "Compiled" alone would spend the
  // whole timeout learning that it failed, and would report a timeout instead
  // of what TeX actually said.
  await expect(status).toHaveText(/^(Compiled|Compile failed)$/, { timeout: 360_000 });
  if ((await status.textContent()) !== "Compiled") {
    const message = await preview.locator(".project-preview-error").first().textContent();
    throw new Error(`The LaTeX compile failed: ${message?.trim()}`);
  }

  const frame = preview.locator("iframe.project-preview-frame");
  await expect(frame).toHaveAttribute("src", /^blob:/);

  // The frame is pointed at a PDF that really is one. Headless Chromium draws
  // nothing inside a PDF viewer, so a screenshot cannot show the page — the
  // bytes are the evidence, and they are the same bytes the download button
  // hands over.
  const src = await frame.getAttribute("src");
  // Waited for rather than read once: the record is filled in by an async read
  // of the Blob, which can land a tick after the status says "Compiled".
  const record = await page.waitForFunction(
    (url) => window.__pdfBlobs?.find((entry) => entry.url === url),
    src,
  );
  const pdf = await record.jsonValue();
  // waitForFunction only resolves on a hit, so this is narrowing, not doubt.
  if (!pdf) throw new Error(`No PDF blob was recorded for ${src}`);
  expect(pdf.magic).toBe("%PDF-");
  // The starter typesets a title page, an abstract, two sections, two display
  // equations and a bibliography. A PDF smaller than this is not that document
  // — the real one comes out around 140 KB.
  expect(pdf.size).toBeGreaterThan(20_000);

  await page.screenshot({ path: "test-results/latex-project-pdf.png" });

  // Specs share one workspace and run in file order, so anything created here
  // has to go — a stray project makes the next spec's project lookups
  // ambiguous, and that failure gets reported against code it does not own.
  await page.getByRole("button", { name: "Delete project" }).click();
  await expect(page.getByRole("heading", { name: "E2E TeX Paper" })).toHaveCount(0);
});
