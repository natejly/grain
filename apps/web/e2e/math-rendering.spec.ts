import { expect, test } from "@playwright/test";
import { openView } from "./shell";

// Chat rendered TeX source as literal text: views/chat.tsx wired ReactMarkdown
// with no remark-math/rehype-katex, while views/documents.tsx had both. The
// stylesheet was also imported from inside documents.tsx, so maths only had
// styling once you had visited that view. Both are invisible to every unit
// test, because the pipeline is assembled in JSX and never exercised there.
test("chat renders LaTeX maths, not TeX source", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/");

  // The composer stays disabled until the workspace has a source, so seed one.
  await openView(page, "Library", /Sources/);
  await page.locator('input[type="file"]').setInputFiles({
    name: "math-e2e.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# Maths\n\nA note so the composer unlocks.\n"),
  });
  // Scoped to THIS spec's row: sources come back newest-first, so `.last()` is
  // the oldest and is already indexed whenever an earlier spec left one.
  await expect(
    page.locator(".source-row", { hasText: "math-e2e.md" }).getByText("Indexed"),
  ).toBeVisible({ timeout: 30_000 });

  await page.getByRole("button", { name: "Chat", exact: true }).click();
  const composer = page.getByRole("textbox", { name: "Message" });
  await composer.fill("show me the quadratic formula");
  await composer.press("Enter");

  const body = page.locator(".message-body").last();
  // Two maths in the scripted reply: inline $E = mc^2$ and a displayed
  // quadratic formula. Both must reach KaTeX.
  await expect(body.locator(".katex").first()).toBeVisible({ timeout: 60_000 });
  // Deliberately NOT asserting that "\\frac" is absent from the text: KaTeX
  // embeds the original TeX in a MathML <annotation> for screen readers and
  // copy-paste, so it legitimately survives in textContent. The presence of a
  // .katex element is the real signal — before the fix there was none, because
  // ReactMarkdown emitted the source as a plain paragraph.
  await expect(body.locator(".katex")).toHaveCount(2);

  // Clean up the source this spec created. Specs share one workspace and run in
  // file order, so a stray row makes the next spec's getByTitle("Delete source")
  // a strict-mode violation — which is a failure in *that* spec, reported
  // against code it does not own. Scoped to this row by filename rather than
  // .first(), so it stays correct whatever else the workspace holds.
  await openView(page, "Library", /Sources/);
  // Deletion is confirm()-gated, so the handler has to be armed before the
  // click or the click never resolves.
  page.once("dialog", (dialog) => dialog.accept());
  const row = page.locator(".source-row", { hasText: "math-e2e.md" });
  await row.getByTitle("Delete source").click();
  await expect(page.getByText("math-e2e.md")).toHaveCount(0);
});
