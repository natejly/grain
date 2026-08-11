"use client";

import type { DashboardSpec, DatasetQueryResult } from "@workspace/api-client";
import {
  categoryField,
  chartSeries,
  formatValue,
  type ChartPoint,
  type ChartSeries,
} from "./views/dashboard-format";

/**
 * What a dashboard actually looks like.
 *
 * Four forms, one hue. The palette has exactly one accent and this file may not
 * invent a second — so no form here relies on colour to tell two things apart:
 * a bar chart draws one measure at a time, several measures become small
 * multiples rather than a second colour on the same axis, and the donut's
 * segments are identified by direct labels with the ring's opacity ramp only
 * ordering them. That is the constraint, but it is also the right answer: two
 * measures sharing an axis is the most common way a chart lies about its data.
 *
 * Every form ships the numbers as a table too, for a screen reader and for
 * anyone the shapes fail. It is visually hidden rather than absent because a
 * chart nobody can read is not an accessible chart with a caption.
 */

type ChartProps = {
  spec: DashboardSpec;
  result: DatasetQueryResult;
  /** Names the figure. Required: a chart with no accessible name is a blank. */
  title: string;
};

function DataTable({
  columns,
  rows,
  className,
}: {
  columns: string[];
  rows: Record<string, unknown>[];
  className?: string;
}) {
  return (
    <div className={className ?? "result-table-wrap"}>
      <table className="result-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td key={column}>{String(row[column] ?? "—")}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The same numbers the shapes above encode, for anyone the shapes fail. */
function SeriesTable({ category, series }: { category: string; series: ChartSeries }) {
  return (
    <DataTable
      className="visually-hidden"
      columns={[category, series.field]}
      rows={series.points.map((point) => ({
        [category]: point.label,
        [series.field]: point.value,
      }))}
    />
  );
}

function BarSeries({ category, series }: { category: string; series: ChartSeries }) {
  const scale = Math.max(series.max, 1);
  return (
    <figure className="chart-figure">
      <figcaption>{series.field}</figcaption>
      <div className="mini-chart bar" role="presentation">
        {series.points.map((point, index) => (
          <div className="mini-bar-row" key={`${point.label}-${index}`}>
            <span title={point.label}>{point.label}</span>
            <div>
              <i style={{ width: `${Math.max(2, (point.value / scale) * 100)}%` }} />
            </div>
            <strong>{formatValue(point.value)}</strong>
          </div>
        ))}
      </div>
      <SeriesTable category={category} series={series} />
    </figure>
  );
}

/**
 * A line over the rows in the order the query returned them.
 *
 * `vector-effect` keeps the 2px stroke 2px after the viewBox is stretched to
 * the tile; without it a wide short tile draws a hairline and a tall one draws
 * a slab. Only the first and last points are labelled — a number on every point
 * is a table pretending to be a chart.
 */
function LineSeries({ category, series }: { category: string; series: ChartSeries }) {
  const points = series.points;
  const high = Math.max(series.max, 1);
  const low = points.reduce((least, point) => Math.min(least, point.value), 0);
  const span = high - low || 1;
  const step = points.length > 1 ? 100 / (points.length - 1) : 0;
  const path = points
    .map((point, index) => `${index * step},${40 - ((point.value - low) / span) * 36}`)
    .join(" ");

  return (
    <figure className="chart-figure">
      <figcaption>{series.field}</figcaption>
      {points.length === 0 ? (
        <p className="chart-empty">No rows.</p>
      ) : (
        <>
          <svg
            className="line-chart"
            viewBox="0 0 100 40"
            preserveAspectRatio="none"
            role="presentation"
          >
            <polyline
              points={path}
              vectorEffect="non-scaling-stroke"
              fill="none"
            />
            {points.length === 1 && (
              <circle cx="0" cy={40 - ((points[0].value - low) / span) * 36} r="1.5" />
            )}
          </svg>
          <div className="line-ends">
            <span>
              {points[0].label} · {formatValue(points[0].value)}
            </span>
            {points.length > 1 && (
              <span>
                {points[points.length - 1].label} ·{" "}
                {formatValue(points[points.length - 1].value)}
              </span>
            )}
          </div>
        </>
      )}
      <SeriesTable category={category} series={series} />
    </figure>
  );
}

/** Opacity, not hue: one accent, stepped so the ring reads as an order. */
const RING_STEPS = [1, 0.84, 0.68, 0.54, 0.42, 0.32, 0.24];

/**
 * Part-to-whole, with the tail folded in.
 *
 * A ring with fourteen slices is unreadable and unlabelable, so anything past
 * the sixth becomes "Other" — which is honest, unlike drawing fourteen wedges
 * nobody can match to a legend. Negative values have no share of a whole, so a
 * series containing one falls back to bars rather than drawing a nonsense ring.
 */
function DonutSeries({ category, series }: { category: string; series: ChartSeries }) {
  if (series.points.some((point) => point.value < 0) || series.total <= 0) {
    return <BarSeries category={category} series={series} />;
  }
  const ranked = [...series.points].sort((a, b) => b.value - a.value);
  const head = ranked.slice(0, RING_STEPS.length - 1);
  const tail = ranked.slice(RING_STEPS.length - 1);
  const slices: ChartPoint[] = tail.length
    ? [
        ...head,
        { label: "Other", value: tail.reduce((sum, point) => sum + point.value, 0) },
      ]
    : head;

  const radius = 40;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;

  return (
    <figure className="chart-figure donut-figure">
      <figcaption>{series.field}</figcaption>
      <div className="donut-layout">
        <svg className="donut-chart" viewBox="0 0 100 100" role="presentation">
          {slices.map((slice, index) => {
            const length = (slice.value / series.total) * circumference;
            // Two units of the ring are given back as a gap so neighbouring
            // segments read as two shapes rather than one long one.
            const drawn = Math.max(0, length - 2);
            const dash = `${drawn} ${circumference - drawn}`;
            const element = (
              <circle
                key={`${slice.label}-${index}`}
                cx="50"
                cy="50"
                r={radius}
                fill="none"
                strokeWidth="16"
                strokeDasharray={dash}
                strokeDashoffset={-offset}
                opacity={RING_STEPS[index] ?? RING_STEPS[RING_STEPS.length - 1]}
              />
            );
            offset += length;
            return element;
          })}
        </svg>
        <ul className="donut-legend">
          {slices.map((slice, index) => (
            <li key={`${slice.label}-${index}`}>
              <i
                aria-hidden="true"
                style={{ opacity: RING_STEPS[index] ?? 0.24 }}
              />
              <span>{slice.label}</span>
              <strong>{Math.round((slice.value / series.total) * 100)}%</strong>
            </li>
          ))}
        </ul>
      </div>
      <SeriesTable category={category} series={series} />
    </figure>
  );
}

export function DashboardChart({ spec, result, title }: ChartProps) {
  if (result.rows.length === 0) {
    return <p className="chart-empty">This query returned no rows.</p>;
  }
  if (spec.visualization === "table") {
    return <DataTable columns={result.columns} rows={result.rows} />;
  }
  const series = chartSeries(spec, result);
  if (series.length === 0) {
    // The spec asks for a shape and names no column this query returns. Drawing
    // it anyway would be a chart of zeroes, which looks like an answer.
    return <DataTable columns={result.columns} rows={result.rows} />;
  }
  const category = categoryField(spec, result);
  const Series =
    spec.visualization === "line"
      ? LineSeries
      : spec.visualization === "donut"
        ? DonutSeries
        : BarSeries;

  return (
    <div
      className="dashboard-chart"
      role="group"
      aria-label={`${title}: ${spec.visualization} of ${series
        .map((item) => item.field)
        .join(", ")} by ${category}`}
    >
      {series.map((item) => (
        <Series key={item.field} category={category} series={item} />
      ))}
    </div>
  );
}
