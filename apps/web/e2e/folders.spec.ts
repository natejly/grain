import { expect, test, type Page } from "@playwright/test";
import { openView } from "./shell";

/**
 * Filing, from the outside.
 *
 * These specs share one workspace and run in file order, so everything created
 * here is deleted before the file returns — and the two kinds of delete behave
 * differently on purpose, which is what the arming below turns on. A *document*
 * is confirm()-gated, because deleting one destroys its version history and
 * there is nothing to re-upload. A *folder* is not, because the server refuses
 * to delete one that still holds files: the only folder delete that can succeed
 * destroys nothing, and a dialog in front of it would guard nothing while
 * training the user to dismiss the dialogs that do.
 *
 * The unwinding at the end of each test is therefore: move the file out, delete
 * the file (armed), delete the folder (not armed).
 */

const FOLDER = "E2E Filing";
const NESTED = "E2E Nested";
const FILE = "E2E Filed Note";

/** The tree's sidebar. Scoped, because "Files" also names the tab strip. */
const tree = (page: Page) => page.locator(".documents-list");

/**
 * A file's own row, anchored.
 *
 * The row's accessible name is "<title> <kind>" and its menu trigger's is
 * "Move <title>", so an unanchored `{ name: title }` matches both and fails on
 * strict mode. Anchoring at the start keeps the row and excludes the trigger,
 * which is the same reason the folder rows below are matched with `^`.
 */
const fileRow = (page: Page, title: string) =>
  tree(page).getByRole("button", { name: new RegExp(`^${title}`) });

async function openFiles(page: Page) {
  await page.goto("/");
  await openView(page, "Files", /^Files/);
  await expect(page.locator(".documents-layout")).toBeVisible();
}

async function makeFolder(page: Page, name: string) {
  await tree(page).getByRole("button", { name: "New folder" }).click();
  await tree(page).getByRole("textbox", { name: "Folder name" }).fill(name);
  await tree(page).getByRole("button", { name: "Create folder" }).click();
  await expect(tree(page).getByRole("button", { name: new RegExp(`^${name}`) }))
    .toBeVisible();
}

async function deleteFolder(page: Page, name: string) {
  await tree(page).getByRole("button", { name: `Actions for ${name}` }).click();
  await page
    .getByRole("group", { name: `Actions for ${name}` })
    .getByRole("button", { name: "Delete folder" })
    .click();
}

test("a folder is created, holds a file, and says how many", async ({ page }) => {
  await openFiles(page);
  await makeFolder(page, FOLDER);

  // "New file here" is the reason folder creation is not in the Create menu:
  // the one thing worth saying about a new file is where it goes, and that
  // sentence can only be said from a row.
  await tree(page).getByRole("button", { name: `Actions for ${FOLDER}` }).click();
  await page
    .getByRole("group", { name: `Actions for ${FOLDER}` })
    .getByRole("button", { name: "New file here" })
    .click();
  // The form says where the file will land before it is made, not after.
  await expect(tree(page).getByText(`In ${FOLDER}`)).toBeVisible();
  await tree(page).getByRole("textbox", { name: "Title" }).fill(FILE);
  await tree(page).getByRole("button", { name: "Create", exact: true }).click();

  // The editor opened on the new file, and the header says which folder it is
  // being edited out of — a tree can be scrolled away from the row you clicked.
  await expect(page.getByRole("heading", { name: FILE })).toBeVisible();
  await expect(page.locator(".document-head")).toContainText(FOLDER);

  // The count on the folder row is the number that will block its delete, so it
  // has to be right rather than merely present.
  const row = tree(page).getByRole("button", { name: new RegExp(`^${FOLDER}`) });
  await expect(row).toContainText("1 file");
  await page.screenshot({ path: "test-results/files-tree.png", fullPage: true });

  // Collapsed by default; expanding is what reveals the file.
  await expect(row).toHaveAttribute("aria-expanded", "false");
  await row.click();
  await expect(row).toHaveAttribute("aria-expanded", "true");
  await expect(fileRow(page, FILE)).toBeVisible();

  // --- Put the shared workspace back -------------------------------------
  await tree(page).getByRole("button", { name: `Move ${FILE}` }).click();
  await page
    .getByRole("group", { name: `Move ${FILE}` })
    .getByRole("button", { name: "Top level" })
    .click();
  await expect(row).not.toContainText("file");

  // Armed here and only here: a document delete is confirm()-gated.
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete document" }).click();
  await expect(page.getByText(FILE)).toHaveCount(0);

  // No arm: a folder delete is not gated, and a handler with nothing to catch
  // would stay live for whatever dialog came next.
  await deleteFolder(page, FOLDER);
  await expect(tree(page).getByText(FOLDER)).toHaveCount(0);
});

test("a folder holding a file refuses to be deleted, and says what is in it", async ({
  page,
}) => {
  await openFiles(page);
  await makeFolder(page, FOLDER);

  // A file made at the top level, then filed — the other half of the move.
  await tree(page).getByRole("button", { name: "New document" }).click();
  await tree(page).getByRole("textbox", { name: "Title" }).fill(FILE);
  await tree(page).getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByRole("heading", { name: FILE })).toBeVisible();

  await tree(page).getByRole("button", { name: `Move ${FILE}` }).click();
  await page
    .getByRole("group", { name: `Move ${FILE}` })
    .getByRole("button", { name: FOLDER, exact: true })
    .click();
  await expect(tree(page).getByRole("button", { name: new RegExp(`^${FOLDER}`) }))
    .toContainText("1 file");

  // The decision this feature turns on: refuse, do not cascade. A cascade would
  // need a dialog reading "delete 1 file?" about a file that is not on screen —
  // a question the user cannot answer at the moment it is asked.
  await deleteFolder(page, FOLDER);
  const toast = page.locator(".error-toast");
  await expect(toast).toBeVisible();
  await expect(toast).toContainText("still holds 1 file");
  await expect(toast).toContainText("Move or delete them first");
  await page.screenshot({ path: "test-results/folder-delete-refused.png", fullPage: true });

  // The folder and the file both survived the refusal.
  await expect(tree(page).getByRole("button", { name: new RegExp(`^${FOLDER}`) }))
    .toBeVisible();
  await page.reload();
  await openView(page, "Files", /^Files/);
  await expect(tree(page).getByRole("button", { name: new RegExp(`^${FOLDER}`) }))
    .toContainText("1 file");

  // --- Put the shared workspace back, by the route the refusal described ---
  await tree(page).getByRole("button", { name: new RegExp(`^${FOLDER}`) }).click();
  await fileRow(page, FILE).click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete document" }).click();
  await expect(page.getByText(FILE)).toHaveCount(0);

  await deleteFolder(page, FOLDER);
  await expect(tree(page).getByText(FOLDER)).toHaveCount(0);
});

test("folders nest, rename in place, and move without offering illegal homes", async ({
  page,
}) => {
  await openFiles(page);
  await makeFolder(page, FOLDER);
  await makeFolder(page, NESTED);

  // Move NESTED inside FOLDER, from the row's own menu.
  await tree(page).getByRole("button", { name: `Actions for ${NESTED}` }).click();
  const menu = page.getByRole("group", { name: `Actions for ${NESTED}` });
  await expect(menu.getByText("Move to")).toBeVisible();
  await menu.getByRole("button", { name: FOLDER, exact: true }).click();

  const parent = tree(page).getByRole("button", { name: new RegExp(`^${FOLDER}`) });
  await parent.click();
  await expect(tree(page).getByRole("button", { name: new RegExp(`^${NESTED}`) }))
    .toBeVisible();

  // The move menu must never offer a destination the server will refuse. A
  // folder cannot be moved inside itself or its own descendant, so FOLDER's
  // menu must not list NESTED — a menu whose options fail is a menu people
  // stop reading.
  await tree(page).getByRole("button", { name: `Actions for ${FOLDER}` }).click();
  const parentMenu = page.getByRole("group", { name: `Actions for ${FOLDER}` });
  await expect(parentMenu.getByRole("button", { name: NESTED, exact: true }))
    .toHaveCount(0);
  await expect(parentMenu.getByRole("button", { name: FOLDER, exact: true }))
    .toHaveCount(0);

  // Rename happens in the row, not in a window.prompt(): a native dialog covers
  // the tree, so it cannot show which of several rows is being renamed.
  await parentMenu.getByRole("button", { name: "Rename" }).click();
  const renamed = `${FOLDER} Renamed`;
  await tree(page).getByRole("textbox", { name: "Folder name" }).fill(renamed);
  await tree(page).getByRole("button", { name: "Rename" }).click();
  await expect(tree(page).getByRole("button", { name: new RegExp(`^${renamed}`) }))
    .toBeVisible();
  await expect(tree(page).getByText(FOLDER, { exact: true })).toHaveCount(0);

  // --- Put the shared workspace back --------------------------------------
  // Deleting the outer folder takes the empty one inside it: nothing can be
  // lost with an empty folder, and requiring a delete per level to dismantle an
  // empty tree is busywork rather than a safeguard.
  await deleteFolder(page, renamed);
  await expect(tree(page).getByText(renamed)).toHaveCount(0);
  await expect(tree(page).getByText(NESTED)).toHaveCount(0);
});
