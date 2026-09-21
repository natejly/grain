import { describe, expect, it } from "vitest";
import {
  CSV_PREVIEW_MAX_BYTES,
  csvPreviewRows,
  previewKindOf,
} from "../components/views/attachment-preview";

describe("previewKindOf", () => {
  it("branches on the server-decided mime, never a filename", () => {
    expect(previewKindOf("image/png", 5000)).toBe("image");
    expect(previewKindOf("image/jpeg", 5000)).toBe("image");
    expect(previewKindOf("application/pdf", 5000)).toBe("pdf");
    expect(previewKindOf("text/csv", 5000)).toBe("csv");
    expect(previewKindOf("text/markdown", 5000)).toBe("chip");
    expect(previewKindOf("application/octet-stream", 5000)).toBe("chip");
  });

  it('keeps the chip for media_type "" — a document attachment\'s preview is the editor', () => {
    expect(previewKindOf("", 5000)).toBe("chip");
  });

  it("keeps the chip for an oversized CSV — a peek is not worth the fetch", () => {
    expect(previewKindOf("text/csv", CSV_PREVIEW_MAX_BYTES)).toBe("csv");
    expect(previewKindOf("text/csv", CSV_PREVIEW_MAX_BYTES + 1)).toBe("chip");
  });
});

describe("csvPreviewRows", () => {
  it("parses plain rows into headers and body", () => {
    const preview = csvPreviewRows("a,b,c\n1,2,3\n4,5,6\n");
    expect(preview.headers).toEqual(["a", "b", "c"]);
    expect(preview.rows).toEqual([
      ["1", "2", "3"],
      ["4", "5", "6"],
    ]);
    expect(preview.truncatedRows).toBe(false);
    expect(preview.truncatedCols).toBe(false);
  });

  it("handles quoted commas and escaped quotes", () => {
    const preview = csvPreviewRows('name,quote\n"Doe, Jane","She said ""hi"""\n');
    expect(preview.headers).toEqual(["name", "quote"]);
    expect(preview.rows).toEqual([["Doe, Jane", 'She said "hi"']]);
  });

  it("handles CRLF line endings and a quoted newline", () => {
    const preview = csvPreviewRows('a,b\r\n1,"two\nlines"\r\n3,4\r\n');
    expect(preview.headers).toEqual(["a", "b"]);
    expect(preview.rows).toEqual([
      ["1", "two\nlines"],
      ["3", "4"],
    ]);
  });

  it("tolerates ragged rows as they are", () => {
    const preview = csvPreviewRows("a,b,c\n1\n2,3,4,5\n");
    expect(preview.rows[0]).toEqual(["1"]);
    // The four-cell row is wider than the three headers but under maxCols,
    // so nothing is cut and nothing is invented.
    expect(preview.rows[1]).toEqual(["2", "3", "4", "5"]);
  });

  it("reports a header-only file as headers with no rows", () => {
    const preview = csvPreviewRows("a,b,c\n");
    expect(preview.headers).toEqual(["a", "b", "c"]);
    expect(preview.rows).toEqual([]);
    expect(preview.truncatedRows).toBe(false);
  });

  it("returns nothing for an empty file", () => {
    expect(csvPreviewRows("")).toEqual({
      headers: [],
      rows: [],
      truncatedRows: false,
      truncatedCols: false,
    });
  });

  it("caps rows and reports the cut", () => {
    const body = Array.from({ length: 9 }, (_, i) => `${i},x`).join("\n");
    const preview = csvPreviewRows(`a,b\n${body}\n`, 5);
    expect(preview.rows).toHaveLength(5);
    expect(preview.truncatedRows).toBe(true);
  });

  it("caps columns on every row and reports the cut", () => {
    const preview = csvPreviewRows("a,b,c,d,e,f,g,h\n1,2,3,4,5,6,7,8\n", 5, 6);
    expect(preview.headers).toEqual(["a", "b", "c", "d", "e", "f"]);
    expect(preview.rows[0]).toEqual(["1", "2", "3", "4", "5", "6"]);
    expect(preview.truncatedCols).toBe(true);
  });
});
