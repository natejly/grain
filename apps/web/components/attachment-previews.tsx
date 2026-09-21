"use client";

import { FileText } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "./api";
import { SourceImage } from "./source-image";
import { csvPreviewRows, type CsvPreview } from "./views/attachment-preview";
import { formatBytes } from "./views/shared";

/**
 * The small previews `AttachmentStrip` renders per attachment kind. Each is
 * décor on a chip, never an error surface: anything that cannot load falls
 * back to looking like the plain chip the strip always drew.
 */

/**
 * An image attachment's thumbnail. Wraps `SourceImage` — the existing
 * authenticated object-URL fetcher, which already solves the cross-site
 * cookie problem a bare `<img src={api}>` cannot — sized by
 * `.attachment-thumb` in globals.css.
 */
export function ImageThumb({
  sourceId,
  filename,
}: {
  sourceId: string;
  filename: string;
}) {
  return (
    <span className="attachment-thumb" title={filename}>
      <SourceImage sourceId={sourceId} alt={`Preview of ${filename}`} />
    </span>
  );
}

/** A PDF's label card: badge, filename, honest size. No page-1 render — that
 *  would need pdf.js, a whole dependency for a thumbnail. */
export function PdfCard({
  filename,
  byteSize,
}: {
  filename: string;
  byteSize: number;
}) {
  return (
    <span className="attachment-file-card" title={filename}>
      <span className="attachment-file-badge" aria-hidden="true">
        <FileText size={13} />
        PDF
      </span>
      <span className="attachment-file-name">{filename}</span>
      <span className="attachment-file-size">{formatBytes(byteSize)}</span>
    </span>
  );
}

/**
 * A small CSV's first rows as a table. Fetches the bytes once on mount
 * through the credentialed client (the `SourceImage` liveness pattern), and
 * on ANY failure — fetch, decode, parse, empty file — renders nothing, which
 * leaves the caller's chip standing alone: a preview is décor, never an
 * error surface. The strip's size gate (`CSV_PREVIEW_MAX_BYTES`) bounds the
 * fetch before this component ever mounts.
 */
export function CsvPeek({
  sourceId,
  filename,
}: {
  sourceId: string;
  filename: string;
}) {
  const [preview, setPreview] = useState<CsvPreview | null>(null);
  useEffect(() => {
    let live = true;
    setPreview(null);
    void api
      .sourceContent(sourceId)
      .then((blob) => blob.text())
      .then((text) => {
        if (!live) return;
        const parsed = csvPreviewRows(text);
        if (parsed.headers.length > 0) setPreview(parsed);
      })
      .catch(() => {
        // Silently keep the chip: the file itself is still attached and fine.
      });
    return () => {
      live = false;
    };
  }, [sourceId]);
  if (!preview) return null;
  return (
    <table className="attachment-csv-peek" title={filename}>
      <thead>
        <tr>
          {preview.headers.map((header, index) => (
            <th key={index}>{header}</th>
          ))}
          {preview.truncatedCols && <th aria-label="More columns">…</th>}
        </tr>
      </thead>
      <tbody>
        {preview.rows.map((row, rowIndex) => (
          <tr key={rowIndex}>
            {row.map((cell, cellIndex) => (
              <td key={cellIndex}>{cell}</td>
            ))}
            {preview.truncatedCols && <td>…</td>}
          </tr>
        ))}
        {preview.truncatedRows && (
          <tr>
            <td colSpan={preview.headers.length + (preview.truncatedCols ? 1 : 0)}>
              …
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}
