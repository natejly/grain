import { expect, test } from "@playwright/test";
import { newThread, openView } from "./shell";

/**
 * ⌘K and thread rename — the "fast layer" a daily user lives in.
 *
 * The palette holds nothing exclusive: every row is a faster path to a
 * surface that exists somewhere visible. What these pin is the speed itself —
 * jump anywhere by typing, find a thread by a word in its title, create
 * without visiting the menu — and that renaming makes a thread findable by
 * the name a person will actually remember.
 */

function palette(page: import("@playwright/test").Page) {
  return page.getByRole("dialog", { name: "Command palette" });
}

test("the palette jumps to any view, settings surfaces included", async ({ page }) => {
  await page.goto("/");
  await page.keyboard.press("ControlOrMeta+k");
  await expect(palette(page)).toBeVisible();

  // Empty query answers "what can I do": navigation and creates, no threads.
  await expect(palette(page).getByRole("option", { name: /Chat/ }).first()).toBeVisible();

  await palette(page).getByRole("textbox").fill("memory");
  await page.keyboard.press("Enter");
  await expect(palette(page)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Memory" })).toBeVisible();

  // A settings surface, reachable by typing — the far ones stay near.
  await page.keyboard.press("ControlOrMeta+k");
  await palette(page).getByRole("textbox").fill("mcp");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "MCP servers" })).toBeVisible();
});

test("the palette creates a named thing without visiting the menu", async ({ page }) => {
  await page.goto("/");
  await page.keyboard.press("ControlOrMeta+k");
  await palette(page).getByRole("textbox").fill("new document");
  await page.keyboard.press("Enter");
  // The input becomes the name field; esc would back out one step, not close.
  await expect(palette(page).getByRole("textbox", { name: "Document title" })).toBeVisible();
  await palette(page).getByRole("textbox").fill("Palette Note");
  await page.keyboard.press("Enter");

  await expect(page.getByRole("heading", { name: "Palette Note" })).toBeVisible();

  // Put the shared workspace back.
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete document" }).click();
  await expect(page.getByText("Palette Note")).toHaveCount(0);
});

test("a renamed thread is findable by its new name", async ({ page }) => {
  await page.goto("/");
  await newThread(page);
  const composer = page.getByRole("textbox", { name: "Message" });
  await composer.fill("Who owns Project Northstar?");
  await composer.press("Enter");
  await expect(page.locator(".message.user")).toBeVisible();

  // Rename rides the open thread only, like share.
  await page.getByRole("button", { name: /^Rename / }).click();
  const input = page.getByRole("textbox", { name: /^Rename / });
  await input.fill("Northstar ownership");
  await input.press("Enter");
  await expect(
    page.locator(".thread.active .thread-open"),
  ).toContainText("Northstar ownership");

  // Wander away, then come back by typing the name a person would remember.
  await openView(page, "Library");
  await page.keyboard.press("ControlOrMeta+k");
  await palette(page).getByRole("textbox").fill("northstar own");
  await page.keyboard.press("Enter");
  await expect(page.locator(".chat-layout")).toBeVisible();
  await expect(page.locator(".thread.active .thread-open")).toContainText(
    "Northstar ownership",
  );

  // Put the shared workspace back.
  page.once("dialog", (dialog) => dialog.accept());
  await page
    .getByRole("button", { name: "Delete Northstar ownership" })
    .click();
  await expect(page.getByText("Northstar ownership")).toHaveCount(0);
});
