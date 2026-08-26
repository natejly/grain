import { describe, expect, it } from "vitest";
import { viewableBlob } from "../components/views/sources";

// A `blob:` URL inherits the web app's origin, so an active document (SVG/HTML)
// opened from one runs script on this origin. `viewableBlob` must strip the
// active type from anything that isn't a known-inert format.
describe("viewableBlob", () => {
  it("passes inert types through unchanged", () => {
    for (const type of [
      "application/pdf",
      "image/png",
      "image/jpeg",
      "image/gif",
      "image/webp",
    ]) {
      const blob = new Blob(["x"], { type });
      expect(viewableBlob(blob).type).toBe(type);
    }
  });

  it("neutralizes script-executing types to octet-stream", () => {
    for (const type of [
      "image/svg+xml",
      "text/html",
      "application/xhtml+xml",
      "text/xml",
    ]) {
      const blob = new Blob(["<script>alert(1)</script>"], { type });
      expect(viewableBlob(blob).type).toBe("application/octet-stream");
    }
  });

  it("neutralizes an unknown or empty type", () => {
    expect(viewableBlob(new Blob(["x"], { type: "" })).type).toBe(
      "application/octet-stream",
    );
    expect(
      viewableBlob(new Blob(["x"], { type: "application/x-weird" })).type,
    ).toBe("application/octet-stream");
  });

  it("preserves the underlying bytes when re-wrapping", () => {
    // `new Blob([blob], …)` copies the source bytes; assert the size survives
    // (the runtime's Blob lacks `.text()`, so size is the portable check).
    const original = new Blob(["hello world"], { type: "text/html" });
    expect(viewableBlob(original).size).toBe(original.size);
  });
});
