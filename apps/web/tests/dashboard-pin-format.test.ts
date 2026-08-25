import type { AgentToolCall, Dashboard, ToolArtifact } from "@workspace/api-client";
import { describe, expect, it } from "vitest";
import {
  DASHBOARD_TOOLS,
  dashboardForCall,
  hasChartImage,
  makeDashboardSeed,
} from "../components/views/dashboard-pin-format";

/**
 * The pin bar's resolution rules. The claim that matters most is the refusal:
 * a bar that resolves the wrong dashboard would pin the wrong thing to
 * someone's home screen with one click, in a card crediting the turn that
 * made something else.
 */
function dashboard(id: string, name: string): Dashboard {
  return {
    id,
    name,
    description: "",
    dataset_id: "ds-1",
    spec: { visualization: "bar", query: {}, y_fields: ["revenue"] },
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

function call(overrides: Partial<AgentToolCall> = {}): AgentToolCall {
  return {
    id: "call-1",
    run_id: "run-1",
    conversation_id: "conv-1",
    name: "create_dashboard",
    arguments_json: "{}",
    proposal_preview: "",
    status: "succeeded",
    result_preview: "",
    error: "",
    latency_ms: 0,
    artifacts: [],
    approved_by_mode: "",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function image(mime: string): ToolArtifact {
  return { id: "src-1", kind: "png", mime, bytes: 1024, url: "/api/sources/src-1/content" };
}

const REVENUE_ID = "0f8fad5b-d9cb-469f-a165-70867728950e";
const revenue = dashboard(REVENUE_ID, "Revenue by region");

describe("resolving the dashboard a tool call made", () => {
  it("reads the id out of the server's success line, for every dashboard tool", () => {
    for (const tool of DASHBOARD_TOOLS) {
      const made = call({
        name: tool,
        result_preview: `Created dashboard “Revenue by region” (id ${REVENUE_ID}) showing bars.`,
      });
      expect(dashboardForCall(made, [dashboard("other", "Other"), revenue])?.id).toBe(
        REVENUE_ID,
      );
    }
  });

  it("never resolves by name — a preview without an id is a refusal, not a guess", () => {
    // The name in the arguments can match a PRE-EXISTING dashboard the call
    // never touched (a name-clash create fails against exactly such a name),
    // so a matching name proves nothing about what this call made.
    const made = call({
      name: "bind_dashboard_template",
      arguments_json: JSON.stringify({ name: "  revenue BY region " }),
      result_preview: "Bound “Sales” to that dataset.",
    });
    expect(dashboardForCall(made, [revenue])).toBeNull();
  });

  it("refuses a call whose result is an error line, whatever the status says", () => {
    // Executors report "Error: …" with status still "succeeded". A name-clash
    // error names a dashboard that already exists — resolving it would pin
    // something the call never touched.
    const clashed = call({
      arguments_json: JSON.stringify({ name: "Revenue by region" }),
      result_preview: "Error: a dashboard named “Revenue by region” already exists",
    });
    expect(dashboardForCall(clashed, [revenue])).toBeNull();
  });

  it("only ever offers a pin for a call that succeeded", () => {
    // A proposed or denied create made nothing; a pin button under it would
    // offer to pin a dashboard that does not exist.
    for (const status of ["proposed", "denied", "failed"]) {
      const made = call({
        status,
        result_preview: `Created dashboard “Revenue by region” (id ${REVENUE_ID}) showing bars.`,
      });
      expect(dashboardForCall(made, [revenue])).toBeNull();
    }
  });

  it("refuses to guess rather than resolve the wrong dashboard", () => {
    // An id the list does not hold, and a name that matches nothing: null,
    // not the nearest neighbour.
    const unknownId = call({
      result_preview: "Created dashboard “Gone” (id 11111111-2222-3333-4444-555555555555) showing bars.",
      arguments_json: JSON.stringify({ name: "Gone" }),
    });
    expect(dashboardForCall(unknownId, [revenue])).toBeNull();
    expect(dashboardForCall(call({ result_preview: "Created it." }), [revenue])).toBeNull();
  });

  it("shows nothing for a tool that is not about dashboards", () => {
    const python = call({
      name: "run_python",
      result_preview: `(id ${REVENUE_ID})`,
    });
    expect(dashboardForCall(python, [revenue])).toBeNull();
  });

});

describe("the make-a-dashboard seed", () => {
  it("names the call that drew the chart, so the ask survives a scroll", () => {
    expect(makeDashboardSeed("run_python")).toBe(
      "Turn the chart the run_python call above drew into a dashboard I can pin: ",
    );
  });
});

describe("recognising a chart that is only a picture", () => {
  it("sees an image artifact, whatever raster format the sandbox chose", () => {
    expect(hasChartImage(call({ artifacts: [image("image/png")] }))).toBe(true);
    expect(hasChartImage(call({ artifacts: [image("image/svg+xml")] }))).toBe(true);
  });

  it("does not mistake a non-image artifact or an empty call for a chart", () => {
    expect(hasChartImage(call())).toBe(false);
    expect(hasChartImage(call({ artifacts: [image("application/pdf")] }))).toBe(false);
  });
});
