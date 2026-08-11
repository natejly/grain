"use client";

import type { DashboardPin, DatasetQueryResult } from "@workspace/api-client";
import { GripVertical, PinOff, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { DashboardChart } from "../dashboard-chart";
import {
  GRID_COLUMNS,
  cellsMoved,
  columnWidth,
  describeDashboard,
  layoutChanged,
  placeTile,
  tilesFromPins,
  type Tile,
} from "./dashboard-format";

/**
 * The home screen: the dashboards this user pinned, where this user put them.
 *
 * A tile is a pin. There is no separate "layout" object beside the dashboard,
 * because a second concept would have to be kept in step with the first, and
 * the thing a person arranges *is* the thing they pinned.
 *
 * Dragging is pointer-driven and keyboard-driven, and the keyboard half is not
 * a courtesy: a grid that can only be arranged by dragging is a grid a
 * keyboard user cannot arrange at all. Both halves go through the same pure
 * `placeTile`, so they cannot disagree about where a tile lands.
 */

export type DashboardResultState = {
  status: "loading" | "ready" | "failed";
  result?: DatasetQueryResult;
  error?: string;
};

export type DashboardGridProps = {
  pins: DashboardPin[];
  results: Record<string, DashboardResultState>;
  /** Ask for a dashboard's rows. Called once per tile, and again on Refresh. */
  run: (dashboardId: string, force?: boolean) => void;
  unpin: (dashboardId: string) => Promise<void>;
  saveLayout: (tiles: Tile[]) => Promise<void>;
  /** A dashboard the rail asked to be shown; scrolled to and outlined. */
  focused: string | null;
};

type DragState = {
  id: string;
  mode: "move" | "resize";
  pointerX: number;
  pointerY: number;
  origin: Tile;
  column: number;
};

export function DashboardGrid({
  pins,
  results,
  run,
  unpin,
  saveLayout,
  focused,
}: DashboardGridProps) {
  const [tiles, setTiles] = useState<Tile[]>(() => tilesFromPins(pins));
  const [held, setHeld] = useState<string | null>(null);
  const boardRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const saveTimer = useRef<number | null>(null);
  // What the server last told us. Compared against before every save so an
  // arrow key that changes nothing, or a drag that ends where it began, is not
  // a write.
  const savedRef = useRef<Tile[]>(tilesFromPins(pins));

  useEffect(() => {
    const next = tilesFromPins(pins);
    savedRef.current = next;
    // Only adopt the server's arrangement when it actually differs; a pin list
    // refetched after an unrelated change must not yank a tile out from under
    // a drag in progress.
    setTiles((current) => (layoutChanged(current, next) ? next : current));
  }, [pins]);

  useEffect(() => {
    for (const pin of pins) run(pin.dashboard.id);
  }, [pins, run]);

  const commit = useCallback(
    (next: Tile[]) => {
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
      if (!layoutChanged(savedRef.current, next)) return;
      // Debounced because the keyboard path fires once per keypress and a held
      // arrow key would otherwise be one PUT per repeat.
      saveTimer.current = window.setTimeout(() => {
        savedRef.current = next;
        void saveLayout(next);
      }, 350);
    },
    [saveLayout],
  );

  useEffect(
    () => () => {
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
    },
    [],
  );

  function begin(
    event: React.PointerEvent<HTMLElement>,
    tile: Tile,
    mode: DragState["mode"],
  ) {
    const board = boardRef.current;
    if (!board || event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      id: tile.dashboard_id,
      mode,
      pointerX: event.clientX,
      pointerY: event.clientY,
      origin: tile,
      column: columnWidth(board.clientWidth),
    };
    setHeld(tile.dashboard_id);
  }

  function drag(event: React.PointerEvent<HTMLElement>) {
    const state = dragRef.current;
    if (!state) return;
    const { columns, rows } = cellsMoved(
      event.clientX - state.pointerX,
      event.clientY - state.pointerY,
      state.column,
    );
    // Measured from the origin every time rather than accumulated, so a drag
    // that wanders out and comes back lands where the cursor is.
    const placement =
      state.mode === "move"
        ? {
            grid_x: state.origin.grid_x + columns,
            grid_y: state.origin.grid_y + rows,
          }
        : {
            grid_w: state.origin.grid_w + columns,
            grid_h: state.origin.grid_h + rows,
          };
    setTiles((current) => placeTile(current, state.id, placement));
  }

  function release() {
    if (!dragRef.current) return;
    dragRef.current = null;
    setHeld(null);
    setTiles((current) => {
      commit(current);
      return current;
    });
  }

  function nudge(event: React.KeyboardEvent, tile: Tile, mode: DragState["mode"]) {
    const step: Record<string, [number, number]> = {
      ArrowLeft: [-1, 0],
      ArrowRight: [1, 0],
      ArrowUp: [0, -1],
      ArrowDown: [0, 1],
    };
    const delta = step[event.key];
    if (!delta) return;
    event.preventDefault();
    const [dx, dy] = delta;
    const placement =
      mode === "move"
        ? { grid_x: tile.grid_x + dx, grid_y: tile.grid_y + dy }
        : { grid_w: tile.grid_w + dx, grid_h: tile.grid_h + dy };
    setTiles((current) => {
      const next = placeTile(current, tile.dashboard_id, placement);
      commit(next);
      return next;
    });
  }

  const byId = new Map(pins.map((pin) => [pin.dashboard.id, pin]));

  if (pins.length === 0) {
    return (
      <p className="dashboard-grid-empty">
        Nothing pinned yet. Ask the assistant for a chart, then pin it from
        “All dashboards”.
      </p>
    );
  }

  return (
    <div
      className="dashboard-grid"
      ref={boardRef}
      style={{ gridTemplateColumns: `repeat(${GRID_COLUMNS}, 1fr)` }}
    >
      {tiles.map((tile) => {
        const pin = byId.get(tile.dashboard_id);
        if (!pin) return null;
        const dashboard = pin.dashboard;
        const state = results[dashboard.id];
        return (
          <section
            key={dashboard.id}
            className={[
              "dashboard-pin-tile",
              held === dashboard.id ? "held" : "",
              focused === dashboard.id ? "focused" : "",
            ]
              .filter(Boolean)
              .join(" ")}
            aria-label={dashboard.name}
            id={`pinned-dashboard-${dashboard.id}`}
            style={{
              gridColumn: `${tile.grid_x + 1} / span ${tile.grid_w}`,
              gridRow: `${tile.grid_y + 1} / span ${tile.grid_h}`,
            }}
          >
            <header className="dashboard-pin-head">
              <button
                type="button"
                className="tile-grip"
                aria-label={`Move ${dashboard.name}. Use the arrow keys.`}
                onPointerDown={(event) => begin(event, tile, "move")}
                onPointerMove={drag}
                onPointerUp={release}
                onPointerCancel={release}
                onKeyDown={(event) => nudge(event, tile, "move")}
              >
                <GripVertical size={14} aria-hidden="true" />
              </button>
              <div className="dashboard-pin-title">
                <strong>{dashboard.name}</strong>
                <span>{describeDashboard(dashboard)}</span>
              </div>
              <button
                type="button"
                className="icon-button"
                aria-label={`Refresh ${dashboard.name}`}
                onClick={() => run(dashboard.id, true)}
              >
                <RefreshCw size={13} />
              </button>
              <button
                type="button"
                className="icon-button"
                aria-label={`Unpin ${dashboard.name}`}
                onClick={() => void unpin(dashboard.id)}
              >
                <PinOff size={13} />
              </button>
            </header>

            <div className="dashboard-pin-body">
              {!state || state.status === "loading" ? (
                <p className="chart-empty">Loading…</p>
              ) : state.status === "failed" ? (
                <p className="chart-error" role="status">
                  {state.error}
                </p>
              ) : state.result ? (
                <DashboardChart
                  spec={dashboard.spec}
                  result={state.result}
                  title={dashboard.name}
                />
              ) : null}
            </div>

            <button
              type="button"
              className="tile-resize"
              aria-label={`Resize ${dashboard.name}. Use the arrow keys.`}
              onPointerDown={(event) => begin(event, tile, "resize")}
              onPointerMove={drag}
              onPointerUp={release}
              onPointerCancel={release}
              onKeyDown={(event) => nudge(event, tile, "resize")}
            />
          </section>
        );
      })}
    </div>
  );
}
