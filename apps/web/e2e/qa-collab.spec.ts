import { expect, test, type Page } from "@playwright/test";
import { openView } from "./shell";

/**
 * The document save-conflict banner, end to end: someone else's save lands
 * while you are editing, your save is refused with a 409 rather than silently
 * clobbering theirs, and the banner offers the two honest exits — reload
 * theirs, or overwrite anyway.
 *
 * "Someone else" is played by the browser's own session calling the API
 * directly (the dashboards.spec pattern): a PUT with no base_version_id moves
 * the head exactly the way a second tab, a teammate, or an agent would, while
 * the open editor keeps holding the version it loaded. One browser context
 * throughout — the conflict is about versions, not about cookies.
 */

const API = "http://127.0.0.1:8010";

/** Every write goes through the browser's own session and CSRF token. */
async function callApi(
  page: Page,
  method: string,
  path: string,
  body?: unknown,
): Promise<{ status: number; json: unknown }> {
  return page.evaluate(
    async ({ api, method, path, body }) => {
      const me = await fetch(`${api}/api/auth/me`, { credentials: "include" });
      const csrf = (await me.json()).csrf_token as string;
      const res = await fetch(`${api}${path}`, {
        method,
        credentials: "include",
        headers: {
          "x-csrf-token": csrf,
          "content-type": "application/json",
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const text = await res.text();
      return { status: res.status, json: text ? JSON.parse(text) : null };
    },
    { api: API, method, path, body } as const,
  );
}

/** Make a document from the Library and land in its editor. */
async function newDocument(page: Page, title: string) {
  await openView(page, "Library", /^Documents/);
  await page.getByRole("button", { name: "New document" }).click();
  const form = page.locator(".documents-new");
  await form.getByRole("textbox", { name: "Title" }).fill(title);
  await form.getByRole("combobox", { name: "Format" }).selectOption({ label: "Markdown" });
  await form.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
}

test("a refused save becomes the conflict banner, and 'Reload theirs' shows the newer version", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const TITLE = "Conflict Banner E2E";
  const BASE = "Everyone starts from this line.\n";
  const THEIRS = "Their newer line, saved while you were editing.\n";
  const MINE = "My conflicting line, typed against a stale head.\n";

  await page.goto("/");
  await newDocument(page, TITLE);

  // A first manual save, so the editor is holding a real head version — the
  // base its next save will be preconditioned on.
  await page.locator(".document-source").fill(BASE);
  await page.getByRole("button", { name: /^Save/ }).click();
  await expect(page.getByRole("button", { name: /^Saved/ })).toBeVisible();

  // "Someone else" saves: a PUT without base_version_id moves the head. The
  // open editor is not told — that silence is the scenario.
  const { json: documents } = await callApi(page, "GET", "/api/documents");
  const doc = (documents as { id: string; title: string }[]).find(
    (item) => item.title === TITLE,
  );
  expect(doc).toBeTruthy();
  const put = await callApi(page, "PUT", `/api/documents/${doc!.id}`, {
    content: THEIRS,
  });
  expect(put.status).toBe(200);

  // Now the stale editor saves. The 409's machine detail must arrive as the
  // banner — not as a toast, and above all not as a silent overwrite.
  await page.locator(".document-source").fill(MINE);
  await page.getByRole("button", { name: /^Save/ }).click();

  const banner = page.locator(".document-live-banner.conflict");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("This document changed while you were editing");
  await expect(banner.getByRole("button", { name: "Reload theirs" })).toBeVisible();
  await expect(banner.getByRole("button", { name: "Overwrite anyway" })).toBeVisible();
  // The refused draft is kept on screen while the banner asks — losing the
  // words would make both choices wrong.
  await expect(page.locator(".document-source")).toHaveValue(MINE);
  await page.locator(".documents-layout").screenshot({
    path: "test-results/qa-collab-conflict.png",
  });

  // Take theirs: the editor re-opens the document, shows the newer content,
  // and stands clean — no banner, nothing left to save.
  await banner.getByRole("button", { name: "Reload theirs" }).click();
  await expect(page.locator(".document-source")).toHaveValue(THEIRS);
  await expect(banner).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^Saved/ })).toBeVisible();

  // And the refused save wrote nothing: the head is still theirs.
  const { json: reread } = await callApi(page, "GET", `/api/documents/${doc!.id}`);
  expect((reread as { content: string }).content).toBe(THEIRS);

  // Self-cleaning, the way the other document specs leave the workspace.
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete document" }).click();
  await expect(page.getByText(TITLE)).toHaveCount(0);
});
