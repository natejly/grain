import type { AppDashboardSnapshot } from "@workspace/api-client";

export function Snapshot({ dashboard }: { dashboard: AppDashboardSnapshot }) {
  const { result } = dashboard;
  if (dashboard.visualization === "table") {
    return (
      <div className="result-table-wrap">
        <table className="result-table">
          <thead>
            <tr>
              {result.columns.map((column) => (
                <th key={column}>{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.map((row, index) => (
              <tr key={index}>
                {result.columns.map((column) => (
                  <td key={column}>{String(row[column] ?? "—")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  const xField = dashboard.x_field || result.columns[0];
  const yField = dashboard.y_fields[0] || result.columns[1];
  const values = result.rows.map((row) => Number(row[yField] || 0));
  const maximum = Math.max(...values, 1);
  return (
    <div className={`mini-chart ${dashboard.visualization}`}>
      {result.rows.map((row, index) => {
        const value = values[index] || 0;
        return (
          <div className="mini-bar-row" key={index}>
            <span>{String(row[xField] ?? "—")}</span>
            <div>
              <i style={{ width: `${Math.max(2, (value / maximum) * 100)}%` }} />
            </div>
            <strong>{value.toLocaleString()}</strong>
          </div>
        );
      })}
    </div>
  );
}
