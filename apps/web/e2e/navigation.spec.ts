import { expect, test, type Page } from "@playwright/test";
import { createFromMenu, openSettings, openView, rail, tabs } from "./shell";

/**
 * The shell is three surfaces: a rail of places you work, a Create menu that
 * makes things, and a Settings menu holding the places you configure. What
 * these pin down is the part a user feels — a destination opens somewhere
 * sensible, its siblings are one click away, coming back returns you to where
 * you were, and "Create" creates rather than navigating.
 *
 * All but one of them create nothing; the one that does deletes what it made,
 * because these specs share a workspace and run in file order.
 */

test("the rail is the places you work, and only those", async ({ page }) => {
  await page.goto("/");
  await expect(rail(page).getByRole("button")).toHaveCount(4);
  // Workflows earned a rail slot rather than a place behind Settings: half of
  // what it does is operating a running thing, and an approval waiting since
  // 3am is not something you go looking for in a configuration menu.
  for (const label of ["Chat", "Files", "Knowledge", "Workflows"]) {
    await expect(rail(page).getByRole("button", { name: new RegExp(`^${label}`) }))
      .toBeVisible();
  }
  // Creating is an action, so it is no longer a place you can visit; the
  // subsystems live inside a destination or behind Settings. "Sandbox" is on
  // this list for the sharper reason: running code is a capability, not a
  // destination — you ask for a chart and it appears on the tool card that drew
  // it, the same way you never visit "the database".
  for (const gone of [
    "Create",
    "Sandbox",
    "Projects",
    "Databases",
    "MCP",
    "Integrations",
    "Graph",
    "Connections",
    "Activity",
    "Admin",
  ]) {
    await expect(rail(page).getByRole("button", { name: gone })).toHaveCount(0);
  }
  // The two menus that took the rest of the navigation.
  await expect(page.getByRole("button", { name: "Create new" })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Settings/ })).toBeVisible();
});

/**
 * Each shot below lands in test-results/ so the chrome can be looked at, not
 * just asserted on. "The element exists" has been true of several layouts in
 * this project that were plainly broken on screen.
 */
async function shot(page: Page, name: string) {
  await page.screenshot({ path: `test-results/shell-${name}.png` });
}

test("each destination opens with its siblings in reach", async ({ page }) => {
  await page.goto("/");
  await shot(page, "chat");

  await openView(page, "Files");
  await expect(page.locator(".documents-layout")).toBeVisible();
  // The group is Files, and the siblings that shared it came along. Sandbox is
  // still not among them: it left the product's navigation entirely, and
  // nothing here should offer a way back to it. Lists joined, beside Boards
  // rather than inside them — a todo list is a one-column board, and that is a
  // detail nobody should have to know to find their checklist.
  for (const tab of ["Files", "Projects", "Boards", "Lists", "Dashboards"]) {
    // Anchored: "Boards" is a substring of "Dashboards", and the count badge is
    // part of the accessible name, so neither end can be matched loosely.
    await expect(tabs(page, "Files").getByRole("button", { name: new RegExp(`^${tab}`) }))
      .toBeVisible();
  }
  await expect(tabs(page, "Files").getByRole("button")).toHaveCount(5);
  await expect(tabs(page, "Files").getByRole("button", { name: /Sandbox/ })).toHaveCount(0);

  await shot(page, "files");

  await openView(page, "Knowledge");
  await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
  for (const tab of ["Sources", "Memory", "Graph"]) {
    await expect(tabs(page, "Knowledge").getByRole("button", { name: tab }))
      .toBeVisible();
  }

  await shot(page, "knowledge");

  await openSettings(page, "Connections");
  // Sandbox tools joined this group beside MCP: both register capabilities the
  // agent may call, and neither is a machine you operate from the rail.
  for (const tab of ["Databases", "MCP", "Sandbox tools", "Integrations"]) {
    await expect(tabs(page, "Connections").getByRole("button", { name: tab }))
      .toBeVisible();
  }
  await expect(tabs(page, "Connections").getByRole("button")).toHaveCount(4);
  await tabs(page, "Connections").getByRole("button", { name: /MCP/ }).click();
  await expect(page.getByRole("heading", { name: "MCP servers" })).toBeVisible();

  await shot(page, "connections");

  // Single-view destinations get no strip — a tab bar with one tab is noise.
  await openSettings(page, "Activity");
  await expect(page.locator(".view-tabs")).toHaveCount(0);
  await shot(page, "activity");

  await openSettings(page, "Admin");
  await expect(page.locator(".view-tabs")).toHaveCount(0);
  // Either the caller owns this workspace and gets the panels, or the API says
  // 403 and the view says so in a sentence. Both are the surface working.
  await expect(
    page.getByRole("heading", { name: "Admin" }).or(page.getByText(/Admin is owner-only/)),
  ).toBeVisible();
  await shot(page, "admin");
});

test("Create makes the thing and opens it, rather than going to a list", async ({
  page,
}) => {
  await page.goto("/");
  // Deliberately starting from Chat: the old "Create" took you to the Files
  // *list* and left the making to whatever button you found once you arrived.
  await expect(page.locator(".chat-layout")).toBeVisible();

  await createFromMenu(page, "Document", "Create Menu Note");

  // The document exists and is open for editing — not a list, not a form.
  await expect(page.getByRole("heading", { name: "Create Menu Note" })).toBeVisible();
  await expect(page.locator(".document-source")).toBeVisible();
  // And it landed on the destination that holds files, with its tab strip.
  await expect(tabs(page, "Files").getByRole("button", { name: /^Files/ }))
    .toHaveAttribute("aria-current", "page");

  // Put the shared workspace back: a stray document makes the next spec's
  // document lookups ambiguous.
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete document" }).click();
  await expect(page.getByText("Create Menu Note")).toHaveCount(0);
});

test("Create reaches a sibling of the destination it opens", async ({ page }) => {
  await page.goto("/");
  // Boards are not the landing view of any destination, so this is the case
  // that would break first if the menu navigated instead of creating.
  await createFromMenu(page, "Board", "Create Menu Board");

  await expect(page.getByRole("heading", { name: "Create Menu Board" })).toBeVisible();
  await expect(tabs(page, "Files").getByRole("button", { name: /^Boards/ }))
    .toHaveAttribute("aria-current", "page");

  // No arm: deleting a board is not confirm()-gated, and a handler with nothing
  // to catch stays live for whatever dialog comes next in this test.
  await page.getByRole("button", { name: "Delete Create Menu Board" }).click();
  await expect(page.getByRole("heading", { name: "Create Menu Board" })).toHaveCount(0);
});

test("the Create menu offers every kind of thing", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Create new" }).click();
  const menu = page.getByRole("group", { name: "Create" });
  for (const thing of [
    "Document",
    "Project",
    "LaTeX document",
    "Board",
    "Dashboard",
    "Workflow",
  ]) {
    await expect(menu.getByRole("button", { name: thing, exact: true })).toBeVisible();
  }
  // Two absences, for two different reasons. "Sandbox" was never a thing you
  // make — it was a machine you were being asked to operate. "Folder" is a
  // thing you make, but the only question worth asking about a new one is which
  // folder it goes in, and a menu in the corner of the screen cannot ask that;
  // it is made in the tree, where the answer is whatever you were pointing at.
  for (const absent of ["Sandbox", "Folder"]) {
    await expect(menu.getByRole("button", { name: absent, exact: true })).toHaveCount(0);
  }
  await shot(page, "create-menu");

  // The second step: the one thing about the new object that cannot be filled
  // in later, since none of these can be renamed.
  await menu.getByRole("button", { name: "Document", exact: true }).click();
  await expect(menu.getByRole("textbox", { name: "Document title" })).toBeVisible();
  // The document formats must not offer the word "LaTeX" at all: neither of
  // them compiles anything, and the option that used to say it sent a user
  // looking for a PDF down a path that could never produce one. LaTeX is a
  // *project* kind, one entry up in this same menu.
  const format = menu.getByRole("combobox", { name: "Document format" });
  await expect(format.getByRole("option")).toHaveText(["Markdown", "Plain text"]);
  await expect(format.getByRole("option", { name: /latex/i })).toHaveCount(0);
  await shot(page, "create-menu-document");

  // The open panel's scrim covers the viewport, so Create has to be dismissed
  // before another trigger can be reached — same as it is for a user.
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: /^Settings/ }).click();
  await expect(page.getByRole("group", { name: "Settings" })).toBeVisible();
  await shot(page, "settings-menu");
});

test("a menu closes on Escape with focus back on its trigger, and on a click outside", async ({
  page,
}) => {
  await page.goto("/");
  const create = page.getByRole("button", { name: "Create new" });

  await create.click();
  await expect(page.getByRole("group", { name: "Create" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("group", { name: "Create" })).toHaveCount(0);
  // Focus must come back: the element the user was standing on has just stopped
  // existing, and without this it falls to <body>.
  await expect(create).toBeFocused();
  await expect(create).toHaveAttribute("aria-expanded", "false");

  await page.getByRole("button", { name: /^Settings/ }).click();
  await expect(page.getByRole("group", { name: "Settings" })).toBeVisible();
  // The scrim covers the viewport, so any click outside the panel lands on it.
  await page.mouse.click(400, 400);
  await expect(page.getByRole("group", { name: "Settings" })).toHaveCount(0);
});

test("memory is a first-class surface under Knowledge", async ({ page }) => {
  await page.goto("/");
  await openView(page, "Knowledge", /Memory/);

  await expect(page.getByRole("heading", { name: "Memory" })).toBeVisible();
  // The topbar says which group you are in, so the title is never context-free.
  await expect(page.locator(".page-context")).toContainText("Knowledge");

  // Either it has memories — and then they are searchable and forgettable — or
  // it says so. Both are the surface working; which one depends on the run.
  const rows = page.locator(".memory-row");
  if ((await rows.count()) > 0) {
    await expect(rows.first().getByRole("button", { name: "Forget this memory" }))
      .toBeVisible();
    await page.getByRole("searchbox", { name: "Search memory" }).fill("zzzz-no-match");
    await expect(page.locator(".memory-row")).toHaveCount(0);
    await expect(page.getByText(/No memory matches/)).toBeVisible();
  } else {
    await expect(page.getByText(/Nothing remembered yet/)).toBeVisible();
  }

  // Memory left the graph page when it got its own; it must not be in both.
  await tabs(page, "Knowledge").getByRole("button", { name: "Graph" }).click();
  await expect(page.locator(".memory-row")).toHaveCount(0);
});

test("a destination reopens where you left it", async ({ page }) => {
  await page.goto("/");
  await openView(page, "Knowledge", /Memory/);
  await expect(page.getByRole("heading", { name: "Memory" })).toBeVisible();

  await openView(page, "Chat");
  // Chat was a single-item group and had no tab strip; Agents joined it, so the
  // strip now exists. Asserting its contents rather than its absence keeps what
  // this line was actually for — that switching destinations rebuilds the strip
  // — instead of deleting the check because the shape moved.
  await expect(tabs(page, "Chat").getByRole("button", { name: /^Chat/ })).toBeVisible();
  await expect(tabs(page, "Chat").getByRole("button", { name: /^Agents/ })).toBeVisible();

  await openView(page, "Knowledge");
  await expect(page.getByRole("heading", { name: "Memory" })).toBeVisible();

  // The same memory applies to the destinations behind Settings.
  await openSettings(page, "Connections", /MCP/);
  await expect(page.getByRole("heading", { name: "MCP servers" })).toBeVisible();
  await openView(page, "Chat");
  await openSettings(page, "Connections");
  await expect(page.getByRole("heading", { name: "MCP servers" })).toBeVisible();
});
