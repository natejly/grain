import { expect, test } from "@playwright/test";
import { createFromMenu } from "./shell";

declare global {
  interface Window {
    __pdfBlobs?: { url: string; size: number; magic: string }[];
  }
}

/**
 * LaTeX projects now compile server-side via POST /api/latex/compile, so
 * there is no ~79 MiB wasm download. The timeout is still generous because
 * the first compile pulls a Docker image or runs latexmk on cold caches.
 */
test("Create → LaTeX document compiles to a real PDF", async ({ page }) => {
  test.setTimeout(120_000);

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

  await expect(page.getByRole("heading", { name: "E2E TeX Paper" })).toBeVisible();
  await expect(page.locator(".project-file-name", { hasText: "main.tex" })).toBeVisible();

  const preview = page.locator(".project-preview-pane");
  const status = preview.locator(".project-preview-status");

  await expect(status).toHaveText(/^(Compiled|Compile failed)$/, { timeout: 90_000 });
  if ((await status.textContent()) !== "Compiled") {
    const message = await preview.locator(".project-preview-error").first().textContent();
    throw new Error(`The LaTeX compile failed: ${message?.trim()}`);
  }

  const frame = preview.locator("iframe.project-preview-frame");
  await expect(frame).toHaveAttribute("src", /^blob:/);

  const src = await frame.getAttribute("src");
  const record = await page.waitForFunction(
    (url) => window.__pdfBlobs?.find((entry) => entry.url === url),
    src,
  );
  const pdf = await record.jsonValue();
  if (!pdf) throw new Error(`No PDF blob was recorded for ${src}`);
  expect(pdf.magic).toBe("%PDF-");
  expect(pdf.size).toBeGreaterThan(20_000);

  await page.screenshot({ path: "test-results/latex-project-pdf.png" });

  await page.getByRole("button", { name: "Delete project" }).click();
  await expect(page.getByRole("heading", { name: "E2E TeX Paper" })).toHaveCount(0);
});
