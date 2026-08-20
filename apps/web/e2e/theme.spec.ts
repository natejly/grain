import { expect, test } from "@playwright/test";

/**
 * The theme is three mechanisms that can each fail silently, so each gets an
 * assertion in a real browser: the CSS cascade (bare :root is light, the media
 * query moves to dark, an explicit data-theme beats both), the toggle's write
 * to localStorage, and the pre-paint script that replays that write before the
 * first frame. A jsdom test can check none of them — computed styles here come
 * from an actual cascade with an actual prefers-color-scheme.
 */

const CREAM = "rgb(250, 247, 240)"; // --bg light
const INK = "rgb(11, 13, 16)"; // --bg dark

const bodyBackground = () =>
  document.defaultView!.getComputedStyle(document.body).backgroundColor;

const themeAttribute = () => document.documentElement.getAttribute("data-theme");

test.describe("theme", () => {
  test("defaults to light with no stored choice and no system preference", async ({
    page,
  }) => {
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto("/");
    expect(await page.evaluate(themeAttribute)).toBeNull();
    expect(await page.evaluate(bodyBackground)).toBe(CREAM);
  });

  test("follows a dark system preference without an attribute", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "dark" });
    await page.goto("/");
    // No attribute: the media query alone carries it, which is what lets a
    // viewer's OS switch take effect while the page is open.
    expect(await page.evaluate(themeAttribute)).toBeNull();
    expect(await page.evaluate(bodyBackground)).toBe(INK);
  });

  test("the toggle overrides the system in both directions and persists", async ({
    page,
  }) => {
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto("/");

    await page.getByRole("button", { name: "Switch to dark theme" }).click();
    expect(await page.evaluate(themeAttribute)).toBe("dark");
    expect(await page.evaluate(bodyBackground)).toBe(INK);
    expect(
      await page.evaluate(() => localStorage.getItem("grain-theme")),
    ).toBe("dark");

    // Explicit light must beat a dark system preference too, not just the
    // other way round — that is the half a single [data-theme="dark"] rule
    // would miss.
    await page.emulateMedia({ colorScheme: "dark" });
    await page.getByRole("button", { name: "Switch to light theme" }).click();
    expect(await page.evaluate(themeAttribute)).toBe("light");
    expect(await page.evaluate(bodyBackground)).toBe(CREAM);
  });

  test("applies the stored choice before the first paint", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto("/");
    await page.getByRole("button", { name: "Switch to dark theme" }).click();

    // Read the attribute at document_start on the next load: if the value is
    // already there before any framework code runs, there is no flash.
    const atDocumentStart: string[] = [];
    await page.exposeFunction("recordTheme", (value: string) => {
      atDocumentStart.push(value);
    });
    await page.addInitScript(() => {
      document.addEventListener("readystatechange", () => {
        if (document.readyState === "interactive") {
          // @ts-expect-error injected above
          window.recordTheme(document.documentElement.getAttribute("data-theme"));
        }
      });
    });

    await page.reload();
    expect(atDocumentStart).toEqual(["dark"]);
    expect(await page.evaluate(bodyBackground)).toBe(INK);
  });
});
