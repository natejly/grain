import {
  ApiError,
  WorkflowCompileError,
  type Dashboard,
  type DashboardLayoutTile,
  type DashboardPin,
  type DashboardSpec,
  type DatasetQueryResult,
} from "@workspace/api-client";

/**
 * The arithmetic behind the home screen, with no React in it.
 *
 * Two things live here rather than inside the grid component, and for the same
 * reason: they are the parts that can be *wrong* rather than merely ugly. A
 * layout that lets two tiles claim one cell, or a chart that reads a column the
 * query never returned, fails silently on screen — the tile just looks odd —
 * and a rendering test cannot tell the difference. As plain functions they are
 * checkable in `tests/dashboard-format.test.ts`, and the component keeps only
 * the pointer handling.
 */

/** The API's grid, and the reason every clamp below uses these numbers. */
export const GRID_COLUMNS = 12;
export const MAX_TILE_SPAN = 12;
export const MAX_GRID_ROW = 200;

/** One grid row in CSS pixels. Mirrors `--tile-row` in globals.css. */
export const GRID_ROW_HEIGHT = 76;
/** The gutter between tiles, in pixels. Mirrors the grid's `gap`. */
export const GRID_GAP = 12;

export type Tile = DashboardLayoutTile;

function clamp(value: number, low: number, high: number): number {
  if (!Number.isFinite(value)) return low;
  return Math.min(high, Math.max(low, Math.round(value)));
}

/** A placement the API will accept: inside the grid, and inside the board. */
export function clampTile(tile: Tile): Tile {
  const grid_w = clamp(tile.grid_w, 1, MAX_TILE_SPAN);
  const grid_h = clamp(tile.grid_h, 1, MAX_TILE_SPAN);
  return {
    dashboard_id: tile.dashboard_id,
    grid_w,
    grid_h,
    grid_x: clamp(tile.grid_x, 0, GRID_COLUMNS - grid_w),
    grid_y: clamp(tile.grid_y, 0, MAX_GRID_ROW),
  };
}

function overlaps(a: Tile, b: Tile): boolean {
  return (
    a.grid_x < b.grid_x + b.grid_w &&
    b.grid_x < a.grid_x + a.grid_w &&
    a.grid_y < b.grid_y + b.grid_h &&
    b.grid_y < a.grid_y + a.grid_h
  );
}

/**
 * Resolve a set of placements into a grid where nothing overlaps.
 *
 * The API stores what it is told and does not arbitrate collisions — two tiles
 * *can* be saved onto the same cell, and the browser would then stack them,
 * hiding one behind the other with nothing to say so. So the client settles it,
 * by reading top-to-bottom and floating every tile as far up as it will go.
 *
 * `priority` is the tile the user is currently holding: it wins its row, so
 * dropping a tile onto an occupied row displaces the occupant downwards instead
 * of bouncing the dragged tile back.
 */
export function compactTiles(tiles: Tile[], priority?: string): Tile[] {
  const ordered = [...tiles].sort(
    (a, b) =>
      a.grid_y - b.grid_y ||
      (a.dashboard_id === priority ? -1 : 0) ||
      (b.dashboard_id === priority ? 1 : 0) ||
      a.grid_x - b.grid_x,
  );
  const placed: Tile[] = [];
  for (const tile of ordered) {
    const clamped = clampTile(tile);
    let grid_y = 0;
    while (
      grid_y < MAX_GRID_ROW &&
      placed.some((other) => overlaps(other, { ...clamped, grid_y }))
    ) {
      grid_y += 1;
    }
    // The search stops at the last row the layout PUT will accept. Floating
    // past it is reachable — twelve-high tiles stack one per twelve rows, so a
    // screen of them runs off the bottom — and the whole save then fails
    // validation, losing the drag that caused it along with every other tile's
    // position. A tile parked on the last row overlaps its neighbour, which is
    // visible and recoverable; a 422 is neither.
    placed.push({ ...clamped, grid_y });
  }
  return placed;
}

/** Where the pins say their tiles are, compacted so the board is consistent. */
export function tilesFromPins(pins: DashboardPin[]): Tile[] {
  return compactTiles(
    pins.map((pin) => ({
      dashboard_id: pin.dashboard.id,
      grid_x: pin.grid_x,
      grid_y: pin.grid_y,
      grid_w: pin.grid_w,
      grid_h: pin.grid_h,
    })),
  );
}

/** Put one tile somewhere and let the rest of the board settle around it. */
export function placeTile(
  tiles: Tile[],
  dashboardId: string,
  placement: Partial<Omit<Tile, "dashboard_id">>,
): Tile[] {
  return compactTiles(
    tiles.map((tile) =>
      tile.dashboard_id === dashboardId ? { ...tile, ...placement } : tile,
    ),
    dashboardId,
  );
}

/** True when a layout differs from another, so an unchanged drag saves nothing. */
export function layoutChanged(before: Tile[], after: Tile[]): boolean {
  if (before.length !== after.length) return true;
  const key = (tile: Tile) =>
    `${tile.dashboard_id}:${tile.grid_x},${tile.grid_y},${tile.grid_w},${tile.grid_h}`;
  const seen = new Set(before.map(key));
  return after.some((tile) => !seen.has(key(tile)));
}

/**
 * Pointer travel in pixels to a whole number of cells.
 *
 * Rounded rather than truncated so a tile follows the cursor to the *nearest*
 * cell: with truncation a drag half a column to the left does nothing at all,
 * which reads as a broken drag rather than as a snap.
 */
export function cellsMoved(
  dx: number,
  dy: number,
  columnWidth: number,
): { columns: number; rows: number } {
  const column = columnWidth + GRID_GAP;
  const row = GRID_ROW_HEIGHT + GRID_GAP;
  return {
    columns: column > 0 ? Math.round(dx / column) : 0,
    rows: row > 0 ? Math.round(dy / row) : 0,
  };
}

/** The pixel width of one of the twelve columns inside a board this wide. */
export function columnWidth(boardWidth: number): number {
  return (boardWidth - GRID_GAP * (GRID_COLUMNS - 1)) / GRID_COLUMNS;
}

// --------------------------------------------------------------------------
// Refusals

/**
 * A refusal, as lines a person can act on.
 *
 * The API answers a binding that does not satisfy a template with every finding
 * at once — one per column that is missing, misnamed or the wrong type — and
 * each finding is already a sentence naming the column. Anything less than the
 * whole list is a repair loop: fix one, resubmit, learn the next.
 */
export function refusalLines(caught: unknown): string[] {
  if (caught instanceof WorkflowCompileError) {
    return caught.errors.map((finding) => finding.message);
  }
  if (caught instanceof ApiError && caught.offline) return [];
  if (caught instanceof Error && caught.message) return [caught.message];
  return [];
}

// --------------------------------------------------------------------------
// Charts

export type ChartPoint = { label: string; value: number };

export type ChartSeries = {
  /** The result column this series was read from; also its heading. */
  field: string;
  points: ChartPoint[];
  max: number;
  total: number;
};

/** The category axis: what the spec asked for, or the query's first column. */
export function categoryField(
  spec: DashboardSpec,
  result: DatasetQueryResult,
): string {
  if (spec.x_field && result.columns.includes(spec.x_field)) return spec.x_field;
  return result.columns[0] ?? "";
}

/**
 * The series a spec draws, or none.
 *
 * Returning `[]` is a real answer and the caller renders the table instead: a
 * spec whose `y_fields` name columns this query does not return would otherwise
 * draw a chart of zeroes, which looks like data and is not. That happens for
 * real — a dashboard bound to a dataset whose column was renamed by a later
 * version keeps its old spec — so the empty case is the safe one.
 *
 * Each `y_field` becomes its *own* series with its own scale, and the component
 * draws them as small multiples. Overlaying them would need one colour per
 * series and a shared axis, and two measures on one axis is the single most
 * common way a chart lies about its data.
 */
export function chartSeries(
  spec: DashboardSpec,
  result: DatasetQueryResult,
): ChartSeries[] {
  const x = categoryField(spec, result);
  const declared = spec.y_fields.filter((field) => result.columns.includes(field));
  const fields =
    declared.length > 0
      ? declared
      : // No usable declaration: the first column that is not the category and
        // holds numbers. A table-shaped result falls through to `[]`.
        result.columns
          .filter((column) => column !== x)
          .filter((column) =>
            result.rows.some((row) => typeof row[column] === "number"),
          )
          .slice(0, 1);

  return fields.map((field) => {
    const points = result.rows.map((row) => ({
      label: String(row[x] ?? "—"),
      value: Number(row[field] ?? 0) || 0,
    }));
    return {
      field,
      points,
      max: points.reduce((high, point) => Math.max(high, point.value), 0),
      total: points.reduce((sum, point) => sum + point.value, 0),
    };
  });
}

/** Compact enough for a tile that may be two columns wide. */
export function formatValue(value: number): string {
  if (!Number.isFinite(value)) return "—";
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (Math.abs(value) >= 10_000) return `${Math.round(value / 1000)}k`;
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

/** "Revenue by region · bar" — what a dashboard is, in one line, for a list. */
export function describeDashboard(dashboard: Dashboard): string {
  const spec = dashboard.spec;
  const grouped = spec.query.group_by ? `by ${spec.query.group_by}` : "";
  const measured = (spec.query.metrics ?? [])
    .map((metric) => metric.label)
    .join(", ");
  return [measured || "rows", grouped, `· ${spec.visualization}`]
    .filter(Boolean)
    .join(" ");
}
