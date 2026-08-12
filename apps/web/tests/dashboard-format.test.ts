import { describe, expect, it } from "vitest";
import { ApiError, WorkflowCompileError } from "@workspace/api-client";
import {
  GRID_COLUMNS,
  MAX_GRID_ROW,
  MAX_TILE_SPAN,
  categoryField,
  cellsMoved,
  chartSeries,
  clampTile,
  columnWidth,
  compactTiles,
  describeDashboard,
  formatValue,
  layoutChanged,
  placeTile,
  refusalLines,
  tilesFromPins,
  type Tile,
} from "../components/views/dashboard-format";
import type {
  Dashboard,
  DashboardPin,
  DashboardSpec,
  DatasetQueryResult,
} from "@workspace/api-client";

/**
 * The home screen's arithmetic, which shipped with no tests at all.
 *
 * Two of these functions decide things that fail *silently* on screen — a grid
 * where two tiles claim one cell just looks odd, and a chart reading a column
 * the query never returned draws zeroes that look like data. Neither is
 * something a "does the element exist" test can see, which is why they were
 * pulled out of the component in the first place.
 *
 * The overlap and bounds checks below are written as invariants over generated
 * layouts rather than as a handful of examples, because the failure they guard
 * is combinatorial: it takes a specific collision to produce it, and a fixed
 * example only ever proves the collision someone already thought of.
 */

const tile = (
  dashboard_id: string,
  grid_x: number,
  grid_y: number,
  grid_w = 3,
  grid_h = 2,
): Tile => ({ dashboard_id, grid_x, grid_y, grid_w, grid_h });

function overlapping(tiles: Tile[]): [Tile, Tile] | null {
  for (let i = 0; i < tiles.length; i += 1) {
    for (let j = i + 1; j < tiles.length; j += 1) {
      const a = tiles[i];
      const b = tiles[j];
      if (
        a.grid_x < b.grid_x + b.grid_w &&
        b.grid_x < a.grid_x + a.grid_w &&
        a.grid_y < b.grid_y + b.grid_h &&
        b.grid_y < a.grid_y + a.grid_h
      ) {
        return [a, b];
      }
    }
  }
  return null;
}

/** Exactly the ranges `DashboardLayoutTile` accepts; anything else is a 422. */
function acceptedByApi(item: Tile): boolean {
  return (
    Number.isInteger(item.grid_x) &&
    Number.isInteger(item.grid_y) &&
    Number.isInteger(item.grid_w) &&
    Number.isInteger(item.grid_h) &&
    item.grid_x >= 0 &&
    item.grid_x <= GRID_COLUMNS - 1 &&
    item.grid_y >= 0 &&
    item.grid_y <= MAX_GRID_ROW &&
    item.grid_w >= 1 &&
    item.grid_w <= MAX_TILE_SPAN &&
    item.grid_h >= 1 &&
    item.grid_h <= MAX_TILE_SPAN
  );
}

/** Deterministic pseudo-random, so a failure is reproducible from its seed. */
function generator(seed: number) {
  let state = seed;
  return () => {
    state = (state * 1103515245 + 12345) % 2147483648;
    return state / 2147483648;
  };
}

describe("clampTile", () => {
  it("keeps a tile inside the twelve columns", () => {
    expect(clampTile(tile("a", 11, 0, 6, 2))).toMatchObject({
      grid_x: 6,
      grid_w: 6,
    });
  });

  it("never returns a zero or negative span", () => {
    expect(clampTile(tile("a", 0, 0, 0, -4))).toMatchObject({
      grid_w: 1,
      grid_h: 1,
    });
  });

  it("substitutes the low bound for a non-finite number", () => {
    const nonsense = clampTile(tile("a", Number.NaN, Number.NaN, Number.NaN, 2));
    expect(nonsense.grid_x).toBe(0);
    expect(nonsense.grid_y).toBe(0);
    expect(nonsense.grid_w).toBe(1);
  });

  it("rounds a fractional placement to a whole cell", () => {
    expect(clampTile(tile("a", 2.6, 1.4, 3.5, 2))).toMatchObject({
      grid_x: 3,
      grid_y: 1,
      grid_w: 4,
    });
  });
});

describe("compactTiles", () => {
  it("leaves a layout that already fits exactly where it is", () => {
    const layout = [tile("a", 0, 0, 6, 2), tile("b", 6, 0, 6, 2)];
    expect(compactTiles(layout)).toEqual(layout);
  });

  it("floats a tile up into the gap left above it", () => {
    const [only] = compactTiles([tile("a", 0, 7, 4, 2)]);
    expect(only.grid_y).toBe(0);
  });

  it("pushes the occupant down when the held tile claims its row", () => {
    const before = [tile("a", 0, 0, 6, 2), tile("b", 6, 0, 6, 2)];
    // "b" is dragged on top of "a": b keeps the row, a is displaced.
    const after = placeTile(before, "b", { grid_x: 0 });
    const b = after.find((item) => item.dashboard_id === "b")!;
    const a = after.find((item) => item.dashboard_id === "a")!;
    expect(b.grid_y).toBe(0);
    expect(a.grid_y).toBeGreaterThanOrEqual(b.grid_y + b.grid_h);
    expect(overlapping(after)).toBeNull();
  });

  it("never lets two tiles claim one cell, over many generated layouts", () => {
    for (let seed = 1; seed <= 200; seed += 1) {
      const random = generator(seed);
      const count = 1 + Math.floor(random() * 8);
      const layout = Array.from({ length: count }, (_, index) =>
        tile(
          `d${index}`,
          Math.floor(random() * 14) - 1,
          Math.floor(random() * 10),
          1 + Math.floor(random() * 13),
          1 + Math.floor(random() * 6),
        ),
      );
      const settled = compactTiles(layout, `d${Math.floor(random() * count)}`);
      expect(settled).toHaveLength(count);
      const clash = overlapping(settled);
      expect(clash, `seed ${seed} produced overlapping tiles`).toBeNull();
    }
  });

  it("only ever emits placements the API will accept", () => {
    for (let seed = 1; seed <= 200; seed += 1) {
      const random = generator(seed);
      const count = 1 + Math.floor(random() * 10);
      const layout = Array.from({ length: count }, (_, index) =>
        tile(
          `d${index}`,
          Math.floor(random() * 14),
          Math.floor(random() * 250),
          1 + Math.floor(random() * 13),
          1 + Math.floor(random() * 13),
        ),
      );
      for (const settled of compactTiles(layout)) {
        expect(
          acceptedByApi(settled),
          `seed ${seed}: ${JSON.stringify(settled)} is outside the API's range`,
        ).toBe(true);
      }
    }
  });

  it("keeps the whole board inside the API's rows even when every tile is full height", () => {
    // Twelve wide by twelve high stacks one tile per twelve rows, so a screen
    // full of them runs off the bottom of the range the layout PUT accepts. The
    // grid must still hand back something saveable.
    const layout = Array.from({ length: 30 }, (_, index) =>
      tile(`d${index}`, 0, index * 12, 12, 12),
    );
    for (const settled of compactTiles(layout)) {
      expect(acceptedByApi(settled)).toBe(true);
    }
  });

  it("preserves every tile it was given", () => {
    const layout = [tile("a", 0, 0), tile("b", 3, 0), tile("c", 6, 0)];
    expect(compactTiles(layout).map((item) => item.dashboard_id).sort()).toEqual([
      "a",
      "b",
      "c",
    ]);
  });
});

describe("tilesFromPins", () => {
  const pin = (name: string, grid_x: number, grid_y: number): DashboardPin =>
    ({
      dashboard: { id: name, name } as Dashboard,
      grid_x,
      grid_y,
      grid_w: 6,
      grid_h: 4,
      pinned_at: "2026-01-01T00:00:00Z",
    }) as unknown as DashboardPin;

  it("reads a pin's stored placement", () => {
    expect(tilesFromPins([pin("a", 0, 0)])).toEqual([
      { dashboard_id: "a", grid_x: 0, grid_y: 0, grid_w: 6, grid_h: 4 },
    ]);
  });

  it("settles pins the server stored on top of each other", () => {
    const settled = tilesFromPins([pin("a", 0, 0), pin("b", 0, 0)]);
    expect(overlapping(settled)).toBeNull();
  });
});

describe("layoutChanged", () => {
  const layout = [tile("a", 0, 0), tile("b", 3, 0)];

  it("is false for the same arrangement, so an idle drag saves nothing", () => {
    expect(layoutChanged(layout, [tile("a", 0, 0), tile("b", 3, 0)])).toBe(false);
  });

  it("is false when the same arrangement arrives in a different order", () => {
    expect(layoutChanged(layout, [tile("b", 3, 0), tile("a", 0, 0)])).toBe(false);
  });

  it("notices a move, a resize, and a tile arriving or leaving", () => {
    expect(layoutChanged(layout, [tile("a", 1, 0), tile("b", 3, 0)])).toBe(true);
    expect(layoutChanged(layout, [tile("a", 0, 0, 4), tile("b", 3, 0)])).toBe(true);
    expect(layoutChanged(layout, [tile("a", 0, 0)])).toBe(true);
    expect(layoutChanged(layout, [...layout, tile("c", 6, 0)])).toBe(true);
  });
});

describe("cellsMoved", () => {
  it("snaps to the nearest cell rather than the one already passed", () => {
    // Just over half a column of travel is a move, not a stall.
    expect(cellsMoved(60, 0, 100).columns).toBe(1);
    expect(cellsMoved(40, 0, 100).columns).toBe(0);
  });

  it("reads a drag backwards as a negative move", () => {
    expect(cellsMoved(-224, 0, 100).columns).toBe(-2);
  });

  it("refuses to divide by a board with no width", () => {
    // A board measured at zero (hidden tab, first paint) must not turn a drag
    // into Infinity columns; the vertical axis has a fixed row height and is
    // unaffected.
    expect(cellsMoved(500, 500, -12)).toEqual({ columns: 0, rows: 6 });
  });
});

describe("columnWidth", () => {
  it("subtracts the eleven gutters between twelve columns", () => {
    expect(columnWidth(12 * 50 + 11 * 12)).toBeCloseTo(50);
  });
});

describe("refusalLines", () => {
  it("returns every finding a refused bind reported, not just the first", () => {
    const refusal = new WorkflowCompileError(
      "two problems",
      [
        { code: "template_column_missing", message: "the template requires a number column “value”", node: "value" },
        { code: "template_column_type", message: "“label” is string, not date", node: "label" },
      ],
      [],
    );
    expect(refusalLines(refusal)).toEqual([
      "the template requires a number column “value”",
      "“label” is string, not date",
    ]);
  });

  it("says nothing when the request never reached the server", () => {
    // Status 0 is what `ApiError.offline` reads. A connection that never landed
    // is not a refusal, and printing "Failed to fetch" beside the column selects
    // would read as the template's own complaint about the binding.
    const offline = new ApiError("Network unreachable", 0);
    expect(offline.offline).toBe(true);
    expect(refusalLines(offline)).toEqual([]);
  });

  it("falls back to a plain error's own message", () => {
    expect(refusalLines(new Error("Dashboard name already exists"))).toEqual([
      "Dashboard name already exists",
    ]);
  });

  it("returns nothing it cannot explain", () => {
    expect(refusalLines("nope")).toEqual([]);
    expect(refusalLines(new Error(""))).toEqual([]);
  });
});

describe("chartSeries", () => {
  const spec = (over: Partial<DashboardSpec> = {}): DashboardSpec =>
    ({
      visualization: "bar",
      query: {
        filters: [],
        group_by: "region",
        metrics: [{ field: "amount", operation: "sum", label: "revenue" }],
        order_by: null,
        order_direction: "asc",
        limit: 100,
      },
      x_field: "region",
      y_fields: ["revenue"],
      ...over,
    }) as DashboardSpec;

  const result: DatasetQueryResult = {
    columns: ["region", "revenue"],
    rows: [
      { region: "North", revenue: 165 },
      { region: "South", revenue: 80 },
    ],
    row_count: 2,
    truncated: false,
    elapsed_ms: 1,
  };

  it("reads the declared measure against the declared category", () => {
    const [series] = chartSeries(spec(), result);
    expect(series.field).toBe("revenue");
    expect(series.points).toEqual([
      { label: "North", value: 165 },
      { label: "South", value: 80 },
    ]);
    expect(series.max).toBe(165);
    expect(series.total).toBe(245);
  });

  it("draws nothing rather than zeroes when the spec names a column the query lost", () => {
    // The dataset was re-uploaded with "revenue" renamed; the stored spec still
    // asks for it. A chart of zeroes here would look like a real answer.
    const renamed: DatasetQueryResult = {
      ...result,
      columns: ["region", "takings"],
      rows: [{ region: "North", takings: 165 }],
    };
    const series = chartSeries(spec({ y_fields: ["revenue"] }), renamed);
    expect(series).toHaveLength(1);
    expect(series[0].field).toBe("takings");
  });

  it("returns no series at all for a result with nothing numeric to draw", () => {
    const textual: DatasetQueryResult = {
      columns: ["region", "owner"],
      rows: [{ region: "North", owner: "Ada" }],
      row_count: 1,
      truncated: false,
      elapsed_ms: 1,
    };
    expect(chartSeries(spec({ y_fields: [] }), textual)).toEqual([]);
  });

  it("gives each declared measure its own series and its own scale", () => {
    const two: DatasetQueryResult = {
      columns: ["region", "revenue", "orders"],
      rows: [
        { region: "North", revenue: 165, orders: 3 },
        { region: "South", revenue: 80, orders: 9 },
      ],
      row_count: 2,
      truncated: false,
      elapsed_ms: 1,
    };
    const series = chartSeries(spec({ y_fields: ["revenue", "orders"] }), two);
    expect(series.map((item) => item.field)).toEqual(["revenue", "orders"]);
    expect(series.map((item) => item.max)).toEqual([165, 9]);
  });

  it("treats a missing or non-numeric cell as zero rather than NaN", () => {
    const ragged: DatasetQueryResult = {
      columns: ["region", "revenue"],
      rows: [{ region: "North" }, { region: "South", revenue: "n/a" }],
      row_count: 2,
      truncated: false,
      elapsed_ms: 1,
    };
    const [series] = chartSeries(spec(), ragged);
    expect(series.points.map((point) => point.value)).toEqual([0, 0]);
    expect(Number.isNaN(series.total)).toBe(false);
  });
});

describe("categoryField", () => {
  const result: DatasetQueryResult = {
    columns: ["region", "revenue"],
    rows: [],
    row_count: 0,
    truncated: false,
    elapsed_ms: 0,
  };

  it("falls back to the first column when the spec names one that is not there", () => {
    expect(
      categoryField({ x_field: "territory", y_fields: [] } as unknown as DashboardSpec, result),
    ).toBe("region");
  });
});

describe("formatValue", () => {
  it("shortens the big numbers a narrow tile cannot show in full", () => {
    expect(formatValue(2_400_000)).toBe("2.4M");
    expect(formatValue(24_000)).toBe("24k");
    expect(formatValue(2_400)).toBe("2,400");
  });

  it("has something to print for a number that is not one", () => {
    expect(formatValue(Number.NaN)).toBe("—");
    expect(formatValue(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

describe("describeDashboard", () => {
  it("names the measure, the grouping and the shape", () => {
    expect(
      describeDashboard({
        spec: {
          visualization: "bar",
          query: {
            group_by: "region",
            metrics: [{ field: "amount", operation: "sum", label: "revenue" }],
          },
        },
      } as unknown as Dashboard),
    ).toBe("revenue by region · bar");
  });

  it("says “rows” for a query that measures nothing", () => {
    expect(
      describeDashboard({
        spec: {
          visualization: "donut",
          query: { group_by: "region", metrics: [] },
        },
      } as unknown as Dashboard),
    ).toBe("rows by region · donut");
  });
});
