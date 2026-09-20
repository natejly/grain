import { expect, test, type Page } from "@playwright/test";
import { newThread, openThreadActions, openView } from "./shell";

/**
 * Memory as a surface the member actually controls: write a memory by hand,
 * rewrite it, forget it; turn the whole faculty off from Settings and find it
 * still off after a reload; open a temporary chat and see it say so; and watch
 * a normal run's extraction announce itself.
 *
 * The last one leans on the scripted double: the "remember the atlas launch
 * window" entry in agent-script.json carries a `memories` facet, so the run's
 * post-turn extraction stores a real row through the real pipeline — event,
 * toast, Memory page — with no live provider anywhere near it.
 */

const composer = (page: Page) => page.getByRole("textbox", { name: "Message" });

async function settled(page: Page) {
  await expect(page.getByRole("button", { name: "Stop generating" })).toHaveCount(0);
}

/** The one memory row holding this text — content is this suite's identity. */
const memoryRow = (page: Page, text: string) =>
  page.locator(".memory-row", { hasText: text });

test("a memory can be added by hand, edited inline, and forgotten", async ({
  page,
}) => {
  const ADDED = "The QA fox prefers hazelnut coffee";
  const EDITED = "The QA fox prefers oat-milk coffee";

  await page.goto("/");
  await openView(page, "Library", /Memory/);
  await expect(page.getByRole("heading", { name: "Memory" })).toBeVisible();

  // Add. The form is disclosure-gated; the button reports that state.
  const addButton = page.getByRole("button", { name: "Add memory" });
  await addButton.click();
  await expect(addButton).toHaveAttribute("aria-expanded", "true");
  const form = page.locator(".memory-add-form");
  await form.getByRole("textbox", { name: "New memory" }).fill(ADDED);
  await form.getByRole("combobox", { name: "Kind of memory" }).selectOption("preference");
  await form.getByRole("button", { name: "Save" }).click();

  // The list is re-read from the server after the POST, so the row appearing
  // is the round trip, not an optimistic echo.
  await expect(memoryRow(page, ADDED)).toBeVisible();
  await expect(memoryRow(page, ADDED).locator(".memory-kind")).toHaveText("preference");
  // Unshared by default: a manual add must not widen its own audience.
  await expect(memoryRow(page, ADDED).locator(".memory-scope")).toContainText("personal");

  // Edit in place: the row swaps to a textarea seeded with the sentence.
  await memoryRow(page, ADDED)
    .getByRole("button", { name: "Edit this memory" })
    .click();
  const editArea = page.getByRole("textbox", { name: "Edit this memory" });
  await expect(editArea).toHaveValue(ADDED);
  await editArea.fill(EDITED);
  await page.locator(".memory-edit-area").getByRole("button", { name: "Save" }).click();

  await expect(memoryRow(page, EDITED)).toBeVisible();
  await expect(memoryRow(page, ADDED)).toHaveCount(0);
  await page.screenshot({ path: "test-results/qa-memory-edited.png" });

  // Forget — confirm()-gated like every other destructive control here.
  page.once("dialog", (dialog) => dialog.accept());
  await memoryRow(page, EDITED)
    .getByRole("button", { name: "Forget this memory" })
    .click();
  await expect(memoryRow(page, EDITED)).toHaveCount(0);
});

test("the Settings memory preference persists across a reload", async ({ page }) => {
  await page.goto("/");

  const openMenu = async () => {
    await page.getByRole("button", { name: "Workspace settings" }).click();
    return page.getByRole("group", { name: "Workspace settings" });
  };

  // The harness default: memory on. The control renders only once bootstrap
  // has answered, so waiting for the checkbox is also waiting for the truth.
  let menu = await openMenu();
  const toggle = () => menu.getByLabel("Remember things from my chats");
  await expect(toggle()).toBeChecked();

  // Off. Wait for the PUT to land before trusting the reload below — the
  // optimistic tick answers the click, but only the server survives a reload.
  let saved = page.waitForResponse("**/api/me/memory");
  await toggle().uncheck();
  await saved;
  // The hint states the off-mode boundaries: no recall, no store, no erasure.
  await expect(menu).toContainText("Your runs neither recall nor store memories");
  await page.screenshot({ path: "test-results/qa-memory-pref-off.png" });

  await page.reload();
  menu = await openMenu();
  await expect(toggle()).not.toBeChecked();

  // Flip back — later specs (and the extraction test below) rely on the
  // harness default, so this test must leave the member as it found them.
  saved = page.waitForResponse("**/api/me/memory");
  await toggle().check();
  await saved;
  await expect(menu).toContainText("Your runs recall saved memories");

  await page.reload();
  menu = await openMenu();
  await expect(toggle()).toBeChecked();
  await page.keyboard.press("Escape");
});

test("a temporary chat says it is one, in the composer and on its rail row", async ({
  page,
}) => {
  await page.goto("/");
  await openView(page, "Chat");

  // Incognito is stamped at creation, so the temporary chat has its own door
  // beside "New thread" rather than a toggle it would be too late for.
  await page.getByRole("button", { name: "New temporary chat" }).click();

  // The non-scrolling note above the composer — the indicator that cannot be
  // scrolled away from — and the chip, reporting rather than offering: on a
  // live thread the flag is history, not a setting.
  await expect(page.locator(".incognito-note")).toContainText("Temporary chat");
  const chip = page.getByRole("button", { name: "Incognito" });
  await expect(chip).toHaveAttribute("aria-pressed", "true");
  await expect(chip).toBeDisabled();
  // The rail row carries the ghost, so the thread is legible as temporary
  // from outside the conversation too.
  await expect(
    page.locator(".thread.active").getByLabel("Temporary chat"),
  ).toBeVisible();
  await page.screenshot({ path: "test-results/qa-memory-incognito.png" });

  // Clean up the temporary thread.
  const threads = page.locator(".thread");
  const before = await threads.count();
  page.once("dialog", (dialog) => dialog.accept());
  await openThreadActions(page.locator(".thread.active"));
  await page
    .locator(".thread.active")
    .getByRole("button", { name: /^Delete / })
    .click();
  await expect(threads).toHaveCount(before - 1);
});

test("a run that stores a memory surfaces the notice, and the Memory page holds it", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const PROMPT = "remember the atlas launch window";
  const FACT = "Atlas launch window is March";

  await page.goto("/");
  await newThread(page);
  await composer(page).fill(PROMPT);
  await composer(page).press("Enter");

  // The toast rides the memory.updated workspace event, which lands after the
  // run completes and extraction commits — so it is the thing to wait on
  // first. It self-dismisses after four seconds; the poll below samples far
  // faster than that window.
  await expect(page.locator(".notice-toast")).toContainText("Memory updated", {
    timeout: 60_000,
  });
  await expect(page.locator(".message").last()).toContainText(
    "the Atlas launch window is March",
  );
  await settled(page);

  // The same event refreshed the memories list, so the row is already there
  // when the page opens — no reload, no manual refresh.
  await openView(page, "Library", /Memory/);
  await expect(memoryRow(page, FACT)).toBeVisible();
  // Extraction attributes its provenance: kind, and the thread it learned in.
  await expect(memoryRow(page, FACT).locator(".memory-kind")).toHaveText("fact");
  await page.screenshot({ path: "test-results/qa-memory-extracted.png" });

  // Clean up: forget the extracted fact, then delete the thread that taught it.
  page.once("dialog", (dialog) => dialog.accept());
  await memoryRow(page, FACT)
    .getByRole("button", { name: "Forget this memory" })
    .click();
  await expect(memoryRow(page, FACT)).toHaveCount(0);

  await openView(page, "Chat");
  const row = page.locator(".thread", { hasText: PROMPT }).first();
  await row.locator("button.thread-open").click();
  await expect(page.locator(".thread.active")).toContainText(PROMPT);
  const threads = page.locator(".thread");
  const before = await threads.count();
  page.once("dialog", (dialog) => dialog.accept());
  await openThreadActions(page.locator(".thread.active"));
  await page
    .locator(".thread.active")
    .getByRole("button", { name: /^Delete / })
    .click();
  await expect(threads).toHaveCount(before - 1);
});
