/**
 * Which preview an attachment chip earns — pure and DOM-free, so the branch
 * is testable without rendering the strip.
 *
 * The decision reads the server-decided `media_type` from the enriched
 * attachment row, never the filename's extension: the routing decision was
 * made once, on the server, and re-deriving it client-side is how the two
 * ends drift apart (the `ChatAttachment.kind` doctrine, applied to previews).
 * A document attachment arrives with `media_type: ""` and keeps its chip —
 * the editor is its preview.
 */

/** Above this, a CSV keeps its chip: the peek fetches the file's bytes
 *  client-side, and a preview is not worth a megabyte round trip. */
export const CSV_PREVIEW_MAX_BYTES = 256 * 1024;

export type PreviewKind = "image" | "pdf" | "csv" | "chip";

export function previewKindOf(mediaType: string, byteSize: number): PreviewKind {
  // Defensive at runtime as well as by type: a row cached before the
  // enrichment shipped simply keeps its chip.
  if (!mediaType) return "chip";
  if (mediaType.startsWith("image/")) return "image";
  if (mediaType === "application/pdf") return "pdf";
  if (mediaType === "text/csv" && byteSize <= CSV_PREVIEW_MAX_BYTES) return "csv";
  return "chip";
}

export type CsvPreview = {
  headers: string[];
  rows: string[][];
  truncatedRows: boolean;
  truncatedCols: boolean;
};

/**
 * A minimal RFC-4180-enough parse of a CSV's head: quoted cells, escaped
 * quotes (`""`), CRLF line ends, ragged rows tolerated. Deliberately new
 * rather than shared: the only existing CSV machinery is server-side
 * (`services/analytics._parse_csv`), and dataset previews never see
 * conversation-scoped sources.
 *
 * The first record is the header row; up to `maxRows` records follow. Both
 * axes are capped, and each cap reports itself so the peek can say "…".
 */
export function csvPreviewRows(
  text: string,
  maxRows = 5,
  maxCols = 6,
): CsvPreview {
  const records: string[][] = [];
  let cell = "";
  let row: string[] = [];
  let inQuotes = false;
  let sawAny = false;
  const endRow = () => {
    row.push(cell);
    cell = "";
    records.push(row);
    row = [];
  };
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    sawAny = true;
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          cell += '"';
          i += 1;
        } else {
          inQuotes = false;
        }
      } else {
        cell += ch;
      }
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ",") {
      row.push(cell);
      cell = "";
    } else if (ch === "\n") {
      endRow();
    } else if (ch === "\r") {
      // CRLF: consume the pair as one line end; a lone \r counts too.
      if (text[i + 1] === "\n") i += 1;
      endRow();
    } else {
      cell += ch;
    }
  }
  // A final record with no trailing newline still counts; a trailing newline
  // must not invent an empty one.
  if (cell !== "" || row.length > 0) endRow();
  if (!sawAny || records.length === 0) {
    return { headers: [], rows: [], truncatedRows: false, truncatedCols: false };
  }
  const truncatedCols = records.some((record) => record.length > maxCols);
  const clip = (record: string[]) => record.slice(0, maxCols);
  const headers = clip(records[0]);
  const body = records.slice(1);
  return {
    headers,
    rows: body.slice(0, maxRows).map(clip),
    truncatedRows: body.length > maxRows,
    truncatedCols,
  };
}
