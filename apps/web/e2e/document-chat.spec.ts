import { expect, test, type Page } from "@playwright/test";
import { openView } from "./shell";

/**
 * The document editor's last mile: what each kind promises to render, and the
 * chat panel beside it that can propose an edit you take one hunk at a time.
 *
 * All three facts here are invisible to every unit test, because each one is a
 * pipeline assembled in JSX or a decision that only exists once a real agent
 * run has parked on a real approval:
 *
 *  - "markdown" runs remark-math and rehype-katex and "text" runs *nothing*;
 *  - a turn started in the panel edits the open document without naming it;
 *  - several hunk choices resolve to one version row that says what it did.
 */

/** Make a document from the Files view and land in its editor. */
async function newDocument(page: Page, title: string, format: string) {
  await openView(page, "Library", /^Documents/);
  await page.getByRole("button", { name: "New document" }).click();
  const form = page.locator(".documents-new");
  await form.getByRole("textbox", { name: "Title" }).fill(title);
  await form.getByRole("combobox", { name: "Format" }).selectOption({ label: format });
  await form.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
}

/** Delete the open document; the confirm dialog is armed before the click. */
async function deleteOpenDocument(page: Page, title: string) {
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete document" }).click();
  await expect(page.getByText(title)).toHaveCount(0);
}

// The same source, typed into both kinds. Anything that renders differently
// between the two panes is the kind doing its job; anything that renders the
// same is one of them lying about what it is.
const SOURCE = "# Heading\n\nInline $x^2$ and **bold** prose.\n";

test("markdown renders maths and text renders none of it", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/");

  await newDocument(page, "Kind Markdown E2E", "Markdown");
  await page.locator(".document-source").fill(SOURCE);
  const preview = page.locator(".document-preview");
  // Asserted on `.katex` rather than on the text: KaTeX keeps the original TeX
  // in a MathML <annotation> for screen readers and copy-paste, so "x^2 is
  // absent from textContent" would be false even when the render worked.
  await expect(preview.locator(".katex")).toHaveCount(1);
  await expect(preview.getByRole("heading", { name: "Heading" })).toBeVisible();
  await expect(preview.locator("strong")).toHaveText("bold");
  await page.locator(".document-panes").screenshot({
    path: "test-results/document-kind-markdown.png",
  });
  await deleteOpenDocument(page, "Kind Markdown E2E");

  await newDocument(page, "Kind Text E2E", "Plain text");
  await page.locator(".document-source").fill(SOURCE);
  // "text" means *text*. Running it through ReactMarkdown "just in case" would
  // be the same category of lie the old LaTeX kind told, where the format's
  // name and the format's behaviour were two different things.
  const plain = page.locator(".document-plain");
  await expect(plain).toBeVisible();
  await expect(plain).toHaveText(SOURCE.trim());
  // Presence assertions on the *absence* of a render: no maths, no heading, no
  // emphasis — the three things the markdown pane produced from this same text.
  await expect(page.locator(".document-preview .katex")).toHaveCount(0);
  await expect(page.locator(".document-preview h1")).toHaveCount(0);
  await expect(page.locator(".document-preview strong")).toHaveCount(0);
  await page.locator(".document-panes").screenshot({
    path: "test-results/document-kind-text.png",
  });
  await deleteOpenDocument(page, "Kind Text E2E");
});

test("the side chat edits the open document, one hunk at a time", async ({ page }) => {
  test.setTimeout(180_000);
  await page.goto("/");

  await newDocument(page, "Hunk Review E2E", "Markdown");
  const source = page.locator(".document-source");
  // Two stretches needing the same fix with an untouched line between them, so
  // the proposal segments into exactly two independently decidable hunks.
  await source.fill("Alpha needs review.\nBeta stays.\nGamma needs review.\n");
  await page.getByRole("button", { name: /^Save/ }).click();
  await expect(page.getByRole("button", { name: /^Saved/ })).toBeVisible();

  // Scoped to the document's own toolbar: "Chat" is also the rail's first
  // destination, and an unscoped name would leave the Files view entirely.
  await page
    .locator(".document-actions")
    .getByRole("button", { name: "Chat", exact: true })
    .click();
  const panel = page.locator(".document-chat");
  await expect(panel).toBeVisible();

  // The scripted `edit_document` names neither a document id nor a title. It
  // can only land on this file through the thread's own `document_id`, so a
  // proposal appearing at all is the open-document fallback working.
  const composer = panel.getByRole("textbox", { name: "Message" });
  await composer.fill("sweep the review notes");
  await composer.press("Enter");

  const review = page.getByRole("region", { name: "Proposed changes" });
  await expect(review).toBeVisible({ timeout: 60_000 });
  await expect(review.getByText("2 proposed changes")).toBeVisible();
  await page.locator(".documents-layout").screenshot({
    path: "test-results/document-hunk-review.png",
  });

  // Take the first change and leave the second. The default is "take it whole"
  // — what Approve always meant — so exactly one click is a real amendment.
  await review.getByRole("button", { name: /^Reject change 2/ }).click();
  await expect(review.getByRole("button", { name: /^Accept change 2/ })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  const apply = review.getByRole("button", { name: /^Apply 1 of 2 changes/ });
  await expect(apply).toBeVisible();
  await page.locator(".documents-layout").screenshot({
    path: "test-results/document-hunk-staged.png",
  });
  await apply.click();

  // The document holds the accepted change and *not* the rejected one. Both
  // halves matter: an implementation that applied the whole proposal would pass
  // the first assertion on its own.
  await expect(source).toHaveValue(/Alpha was reviewed\./, { timeout: 60_000 });
  await expect(source).toHaveValue(/Gamma needs review\./);
  await expect(source).not.toHaveValue(/Gamma was reviewed\./);

  // One version for the whole review, and it says what it did. The count is
  // the assertion, not the existence of a row: a reviewer who decided hunk by
  // hunk over the wire would leave a version per click here, a stutter of
  // partial states none of which anyone ever read or could usefully restore.
  await page.getByRole("button", { name: /^History/ }).click();
  const history = page.locator(".document-history");
  await expect(history.getByText(/proposed changes applied/)).toHaveCount(1);
  await expect(
    history.getByText("Sweep the review notes — 1 of 2 proposed changes applied"),
  ).toBeVisible();
  // Two rows total: the manual Save above, and this one. Nothing else wrote.
  await expect(history.locator("li")).toHaveCount(2);
  await page.locator(".document-history").screenshot({
    path: "test-results/document-hunk-version.png",
  });

  // And the turn finished in the panel it was asked in.
  await expect(panel.getByText("Swept the review notes.")).toBeVisible({
    timeout: 60_000,
  });

  await deleteOpenDocument(page, "Hunk Review E2E");
});
