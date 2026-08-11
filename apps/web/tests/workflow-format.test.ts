import { describe, expect, it } from "vitest";
import type { Workflow, WorkflowGraph, WorkflowGraphNode } from "@workspace/api-client";
import {
  argumentRows,
  compileErrorTitle,
  describeReviewability,
  describeSchedule,
  groupFindings,
  layerGraph,
  nodeStatusLabel,
  runIsSettled,
  upstreamOf,
} from "../components/views/workflow-format";

function node(id: string, extra: Partial<WorkflowGraphNode> = {}): WorkflowGraphNode {
  return {
    id,
    kind: "tool",
    description: "",
    tool: "search_sources",
    arguments: {},
    prompt: "",
    ...extra,
  };
}

function graph(
  nodes: WorkflowGraphNode[],
  edges: { from: string; to: string }[] = [],
): WorkflowGraph {
  return {
    name: "Test",
    description: "",
    trigger: { kind: "manual", cron: "", timezone: "UTC" },
    nodes,
    edges,
  };
}

describe("layerGraph", () => {
  it("puts a chain in one node per row", () => {
    expect(
      layerGraph(
        graph(
          [node("a"), node("b"), node("c")],
          [
            { from: "a", to: "b" },
            { from: "b", to: "c" },
          ],
        ),
      ),
    ).toEqual([["a"], ["b"], ["c"]]);
  });

  it("puts independent steps side by side", () => {
    expect(
      layerGraph(
        graph(
          [node("a"), node("b"), node("join")],
          [
            { from: "a", to: "join" },
            { from: "b", to: "join" },
          ],
        ),
      ),
    ).toEqual([["a", "b"], ["join"]]);
  });

  it("pushes a node below its deepest dependency, not its first", () => {
    // `post` depends on `fetch` (row 0) and `summarise` (row 1). Drawing it in
    // row 1 would show it running alongside something it waits for.
    expect(
      layerGraph(
        graph(
          [node("fetch"), node("summarise"), node("post")],
          [
            { from: "fetch", to: "summarise" },
            { from: "fetch", to: "post" },
            { from: "summarise", to: "post" },
          ],
        ),
      ),
    ).toEqual([["fetch"], ["summarise"], ["post"]]);
  });

  it("keeps declaration order inside a row", () => {
    expect(layerGraph(graph([node("z"), node("a"), node("m")]))).toEqual([
      ["z", "a", "m"],
    ]);
  });

  it("ignores edges that name a step the graph does not declare", () => {
    // Otherwise the target sits at indegree 1 forever and never gets a row.
    expect(
      layerGraph(graph([node("a"), node("b")], [{ from: "ghost", to: "b" }])),
    ).toEqual([["a", "b"]]);
  });

  it("still shows every node when a stored graph contains a cycle", () => {
    // The compiler rejects cycles, so this is data arriving from the network
    // rather than something a user can author — but a node the UI silently
    // drops is worse than a node in the wrong row.
    const layers = layerGraph(
      graph(
        [node("a"), node("b"), node("c")],
        [
          { from: "a", to: "b" },
          { from: "b", to: "c" },
          { from: "c", to: "b" },
        ],
      ),
    );
    expect(layers.flat().sort()).toEqual(["a", "b", "c"]);
  });

  it("survives an empty graph", () => {
    expect(layerGraph(graph([]))).toEqual([]);
  });
});

describe("upstreamOf", () => {
  it("names every dependency in declaration order, deduplicated", () => {
    expect(
      upstreamOf(
        graph(
          [node("one"), node("two"), node("sink")],
          [
            { from: "two", to: "sink" },
            { from: "one", to: "sink" },
            { from: "one", to: "sink" },
          ],
        ),
        "sink",
      ),
    ).toEqual(["one", "two"]);
  });
});

describe("argumentRows", () => {
  it("shows a string as itself and anything else as JSON", () => {
    expect(argumentRows({ query: "open PRs", limit: 5, tags: ["a"] })).toEqual([
      ["query", "open PRs"],
      ["limit", "5"],
      ["tags", '["a"]'],
    ]);
  });
});

describe("status vocabulary", () => {
  it("calls a parked node parked", () => {
    expect(nodeStatusLabel("waiting_for_approval")).toBe("Parked for approval");
  });

  it("passes an unknown state through rather than blanking it", () => {
    expect(nodeStatusLabel("something_new")).toBe("something_new");
  });

  it("knows which runs are worth re-fetching", () => {
    expect(runIsSettled("succeeded")).toBe(true);
    expect(runIsSettled("cancelled")).toBe(true);
    expect(runIsSettled("waiting_for_approval")).toBe(false);
    expect(runIsSettled("running")).toBe(false);
  });
});

describe("compile findings", () => {
  it("gives the codes that matter a sentence a person can act on", () => {
    expect(compileErrorTitle("tool_unknown")).toBe(
      "That tool does not exist in this workspace",
    );
    expect(compileErrorTitle("reference_not_upstream")).toBe(
      "A step reads from a step that runs later",
    );
  });

  it("humanises a code it has never seen instead of printing it raw", () => {
    expect(compileErrorTitle("some_future_code")).toBe("Some future code");
  });

  it("groups by step and reads the whole-graph problems first", () => {
    const grouped = groupFindings([
      { code: "tool_unknown", message: "no such tool", node: "post" },
      { code: "graph_cycle", message: "a and b", node: "" },
      { code: "arguments_invalid", message: "bad limit", node: "post" },
    ]);
    expect(grouped.map((group) => group.node)).toEqual(["", "post"]);
    expect(grouped[1].items).toHaveLength(2);
  });
});

describe("describeReviewability", () => {
  it("says a graph that names a write will stop for a person", () => {
    const note = describeReviewability(graph([node("post")]), true);
    expect(note.tone).toBe("parks");
    expect(note.headline).toBe("Stops for a person before it writes");
  });

  it("refuses to call a graph containing an assistant step unattended", () => {
    // The bug a screenshot caught: `requires_approval` is computed from the
    // tool nodes, so a graph whose only write-capable step is an agent came
    // back false and the panel said "read-only — runs unattended" about
    // something that then parked on a write thirty seconds later.
    const note = describeReviewability(
      graph([node("gather"), node("summarise", { kind: "agent", tool: "", prompt: "go" })]),
      false,
    );
    expect(note.tone).toBe("opaque");
    expect(note.headline).toContain("cannot be read off the graph");
  });

  it("says read-only only when every step really is one", () => {
    const note = describeReviewability(graph([node("a"), node("b")]), false);
    expect(note.tone).toBe("unattended");
    expect(note.headline).toContain("runs unattended");
  });
});

describe("describeSchedule", () => {
  const scheduled: Pick<
    Workflow,
    "trigger_kind" | "schedule_cron" | "schedule_timezone" | "status" | "last_dispatched_at"
  > = {
    trigger_kind: "schedule",
    schedule_cron: "0 9 * * 1",
    schedule_timezone: "Europe/London",
    status: "active",
    last_dispatched_at: null,
  };

  it("says a manual workflow runs when you start it", () => {
    const note = describeSchedule(
      { ...scheduled, trigger_kind: "manual", schedule_cron: "" },
      true,
    );
    expect(note.tone).toBe("quiet");
    expect(note.headline).toBe("Runs when you start it");
  });

  it("refuses to call a schedule active when nothing can fire it", () => {
    // The whole point. ADR 0007: a promise the system does not keep is worse
    // than a missing feature.
    const note = describeSchedule(scheduled, false);
    expect(note.tone).toBe("warn");
    expect(note.headline).toContain("nothing will fire it");
    expect(note.detail).toContain("WORKFLOW_CRON_SECRET");
  });

  it("treats an unanswered scheduler as unknown, not as yes", () => {
    const note = describeSchedule(scheduled, null);
    expect(note.tone).toBe("warn");
    expect(note.detail).toContain("treat this as unscheduled");
  });

  it("will not promise a dispatch for a workflow that is not active", () => {
    const note = describeSchedule({ ...scheduled, status: "draft" }, true);
    expect(note.tone).toBe("warn");
    expect(note.headline).toContain("draft");
  });

  it("says it runs, and quotes the cron verbatim, only when it really does", () => {
    const note = describeSchedule(scheduled, true);
    expect(note.tone).toBe("live");
    // Verbatim, not translated: rewriting "0 9 * * 1" as "every Monday at 9am"
    // is one more place to be wrong about the thing this note exists for.
    expect(note.headline).toBe("Runs on 0 9 * * 1 · Europe/London");
    expect(note.detail).toContain("not fired yet");
  });

  it("names the last dispatch when there has been one", () => {
    const note = describeSchedule(
      { ...scheduled, last_dispatched_at: "2026-08-03T09:00:00Z" },
      true,
    );
    expect(note.tone).toBe("live");
    expect(note.detail).toContain("Last dispatched");
  });
});
