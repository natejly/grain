import { describe, expect, it } from "vitest";
import type {
  Workflow,
  WorkflowGraph,
  WorkflowGraphNode,
  WorkflowInputSpec,
  WorkflowNodeRun,
} from "@workspace/api-client";
import {
  argumentRows,
  attributeProblems,
  buildTimeline,
  compileErrorTitle,
  compileWarningTitle,
  describeReviewability,
  describeSchedule,
  draftFromInputs,
  formatDuration,
  groupFindings,
  isBudgetPark,
  layerGraph,
  nodeStatusLabel,
  readInputForm,
  resolvePausedReason,
  runIsSettled,
  upstreamOf,
} from "../components/views/workflow-format";

function nodeRun(overrides: Partial<WorkflowNodeRun> = {}): WorkflowNodeRun {
  return {
    node_key: "step",
    kind: "tool",
    tool_name: "search_sources",
    status: "succeeded",
    attempt: 1,
    arguments: {},
    output: "",
    policy: "allow",
    agent_tool_call_id: null,
    error: "",
    latency_ms: 0,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

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

describe("resolvePausedReason", () => {
  const parked = { status: "waiting_for_approval", paused_reason: "", run_id: "r1" };
  const nothing = { proposed: false, heldByBudget: false };

  it("reads the field when the run carries one", () => {
    expect(resolvePausedReason({ ...parked, paused_reason: "budget" }, nothing)).toBe("budget");
    expect(resolvePausedReason({ ...parked, paused_reason: "approval" }, nothing)).toBe(
      "approval",
    );
  });

  it("falls back to the ceiling's list only when no reason arrived", () => {
    expect(resolvePausedReason(parked, { proposed: false, heldByBudget: true })).toBe("budget");
    expect(resolvePausedReason(parked, nothing)).toBe("");
  });

  it("ignores the ceiling's list for a run that is not parked at all", () => {
    expect(
      resolvePausedReason(
        { status: "running", paused_reason: "", run_id: "r1" },
        { proposed: false, heldByBudget: true },
      ),
    ).toBe("");
  });

  // The regression. A budget park writes no `AgentToolCall`, so a proposed one
  // proves a person is being waited on — and it has to beat a `paused_reason`
  // that still says "budget", because that column is a mirror of the backing
  // run's and lagged by exactly one park when a released graph walked on to a
  // write. Believing it put the spend panel where the Approve button belonged.
  it("lets a proposed call outrank a stale budget reason", () => {
    expect(
      resolvePausedReason({ ...parked, paused_reason: "budget" }, {
        proposed: true,
        heldByBudget: true,
      }),
    ).toBe("approval");
    expect(isBudgetPark("waiting_for_approval", "approval")).toBe(false);
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

  it("names every way a manual step can fail to compile", () => {
    expect(compileErrorTitle("manual_missing_prompt")).toBe(
      "A manual step has no question to ask the person",
    );
    expect(compileErrorTitle("manual_unexpected_tool")).toBe("A manual step also named a tool");
    expect(compileErrorTitle("manual_unexpected_arguments")).toBe(
      "A manual step carries tool arguments",
    );
    expect(compileErrorTitle("manual_field_invalid")).toBe(
      "A manual step declares an unusable field",
    );
    expect(compileErrorTitle("manual_duplicate_field")).toBe(
      "Two of a manual step's fields share a name",
    );
  });

  it("warns that a manual step will pause the run for a person", () => {
    expect(compileWarningTitle("manual_requires_person")).toBe(
      "This step pauses for a person before the run goes on",
    );
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

  it("says a graph with a manual step pauses for a person, not writes", () => {
    // A manual node makes `requires_approval` true too, but the write headline
    // would name a write that never comes. The pause gets its own sentence.
    const note = describeReviewability(
      graph([node("gather"), node("ask", { kind: "manual", tool: "", prompt: "Ready?" })]),
      true,
    );
    expect(note.tone).toBe("parks");
    expect(note.headline).toBe("Pauses for a person before it goes on");
  });

  it("prefers the manual headline over the opaque agent one", () => {
    // A graph with both a manual node and an agent node reads its own tools at
    // run time, but the manual pause is the concrete, known-in-advance stop, so
    // it is named ahead of the agent's unknowable one.
    const note = describeReviewability(
      graph([
        node("ask", { kind: "manual", tool: "", prompt: "Ready?" }),
        node("summarise", { kind: "agent", tool: "", prompt: "go" }),
      ]),
      false,
    );
    expect(note.tone).toBe("parks");
    expect(note.headline).toBe("Pauses for a person before it goes on");
  });
});

describe("the run form", () => {
  function input(extra: Partial<WorkflowInputSpec> = {}): WorkflowInputSpec {
    return {
      name: "repo",
      type: "string",
      label: "",
      description: "",
      required: true,
      default: null,
      choices: [],
      ...extra,
    };
  }

  describe("draftFromInputs", () => {
    it("pre-fills the declared default, because a schedule runs on it", () => {
      expect(
        draftFromInputs([
          input({ default: "acme/api" }),
          input({ name: "dry", type: "boolean", default: true }),
          input({ name: "tags", type: "array", default: ["a"] }),
        ]),
      ).toEqual({ repo: "acme/api", dry: true, tags: '[\n  "a"\n]' });
    });

    it("leaves a field with no default empty rather than inventing one", () => {
      expect(draftFromInputs([input()])).toEqual({ repo: "" });
    });
  });

  describe("readInputForm", () => {
    it("sends a string as typed", () => {
      expect(readInputForm([input()], { repo: "acme/api" })).toEqual({
        payload: { repo: "acme/api" },
        problems: [],
      });
    });

    it("names the input that was left empty", () => {
      const { payload, problems } = readInputForm([input({ label: "Repository" })], {
        repo: "",
      });
      expect(payload).toEqual({});
      expect(problems).toEqual([{ input: "repo", message: "This one is required." }]);
    });

    it("omits an optional empty field rather than sending an empty string", () => {
      // "" is a value, and the server type-checks it: sent for a number input
      // it comes back refused, about a field the person deliberately skipped.
      expect(
        readInputForm([input({ name: "limit", type: "number", required: false })], {
          limit: "",
        }),
      ).toEqual({ payload: {}, problems: [] });
    });

    it("turns a number field into a number, and says so when it cannot", () => {
      const spec = input({ name: "limit", type: "number" });
      expect(readInputForm([spec], { limit: "12.5" }).payload).toEqual({ limit: 12.5 });
      expect(readInputForm([spec], { limit: "many" }).problems).toEqual([
        { input: "limit", message: "Enter a number." },
      ]);
    });

    it("refuses a decimal for an integer input rather than rounding it", () => {
      // The server rejects rather than coerces, so a form that rounded here
      // would run something nobody typed.
      expect(
        readInputForm([input({ name: "limit", type: "integer" })], { limit: "2.5" })
          .problems[0].message,
      ).toContain("whole number");
    });

    it("parses a JSON field and checks it is the shape that was declared", () => {
      const spec = input({ name: "tags", type: "array" });
      expect(readInputForm([spec], { tags: '["a","b"]' }).payload).toEqual({
        tags: ["a", "b"],
      });
      expect(readInputForm([spec], { tags: "{}" }).problems[0].message).toContain("not a list");
      expect(readInputForm([spec], { tags: "[" }).problems[0].message).toContain(
        "not valid JSON",
      );
    });

    it("reads a choice back as the declared value, not as its label", () => {
      // The server refuses "5" where it declared 5 — it rejects rather than
      // coerces — so a select that posted its option text would make every
      // numeric enum unrunnable from the UI.
      const spec = input({ name: "size", type: "integer", choices: [5, 10] });
      expect(readInputForm([spec], { size: "10" }).payload).toEqual({ size: 10 });
    });

    it("always sends a boolean, because an unticked box is false, not absent", () => {
      const spec = input({ name: "dry", type: "boolean" });
      expect(readInputForm([spec], {}).payload).toEqual({ dry: false });
      expect(readInputForm([spec], { dry: true }).payload).toEqual({ dry: true });
    });

    it("collects every problem, not the first", () => {
      const problems = readInputForm(
        [input(), input({ name: "limit", type: "number" })],
        { repo: "", limit: "nope" },
      ).problems;
      expect(problems.map((problem) => problem.input)).toEqual(["repo", "limit"]);
    });
  });

  describe("attributeProblems", () => {
    it("puts the server's sentence next to the box that caused it", () => {
      expect(
        attributeProblems(
          ["input “limit”: 'x' is not of type 'integer'"],
          [input(), input({ name: "limit", type: "integer" })],
        ),
      ).toEqual([
        { input: "limit", message: "input “limit”: 'x' is not of type 'integer'" },
      ]);
    });

    it("reads a required failure, which quotes the name differently", () => {
      expect(
        attributeProblems(["'repo' is a required property"], [input()])[0].input,
      ).toBe("repo");
    });

    it("gives a longer name the problem before a shorter one it contains", () => {
      expect(
        attributeProblems(
          ["input “repo_owner”: too short"],
          [input({ name: "repo" }), input({ name: "repo_owner" })],
        )[0].input,
      ).toBe("repo_owner");
    });

    it("still shows a sentence that names no declared input", () => {
      const attributed = attributeProblems(["something else went wrong"], [input()]);
      expect(attributed).toEqual([{ input: "", message: "something else went wrong" }]);
    });
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

describe("formatDuration", () => {
  it("keeps sub-second spans in milliseconds", () => {
    expect(formatDuration(40)).toBe("40ms");
    expect(formatDuration(999)).toBe("999ms");
  });

  it("carries one decimal only while seconds are small", () => {
    expect(formatDuration(1400)).toBe("1.4s");
    expect(formatDuration(12000)).toBe("12s");
  });

  it("becomes minutes-and-seconds past a minute, carrying a rounded 60", () => {
    expect(formatDuration(123000)).toBe("2m 3s");
    expect(formatDuration(120000)).toBe("2m");
    // 119.6s must not render as "1m 60s".
    expect(formatDuration(119600)).toBe("2m");
  });

  it("renders something for a zero or negative span rather than a gap", () => {
    expect(formatDuration(0)).toBe("0ms");
    expect(formatDuration(-5)).toBe("0ms");
  });
});

describe("buildTimeline", () => {
  const run = { started_at: "2026-01-01T00:00:00.000Z", created_at: "2026-01-01T00:00:00.000Z" };

  it("orders nodes by when they started, not by graph position", () => {
    const { rows } = buildTimeline(run, [
      nodeRun({ node_key: "second", started_at: "2026-01-01T00:00:02.000Z", latency_ms: 500 }),
      nodeRun({ node_key: "first", started_at: "2026-01-01T00:00:01.000Z", latency_ms: 500 }),
    ]);
    expect(rows.map((r) => r.node.node_key)).toEqual(["first", "second"]);
    expect(rows[0].offsetMs).toBe(1000);
    expect(rows[1].offsetMs).toBe(2000);
  });

  it("sorts a node that never ran to the end, with no offset or duration", () => {
    const { rows } = buildTimeline(run, [
      nodeRun({ node_key: "skipped", status: "skipped" }),
      nodeRun({ node_key: "ran", started_at: "2026-01-01T00:00:01.000Z", latency_ms: 200 }),
    ]);
    expect(rows.map((r) => r.node.node_key)).toEqual(["ran", "skipped"]);
    expect(rows[1].offsetMs).toBeNull();
    expect(rows[1].durationMs).toBeNull();
  });

  it("falls back to finished minus started when latency was not recorded", () => {
    const { rows } = buildTimeline(run, [
      nodeRun({
        node_key: "a",
        started_at: "2026-01-01T00:00:01.000Z",
        finished_at: "2026-01-01T00:00:03.500Z",
        latency_ms: 0,
      }),
    ]);
    expect(rows[0].durationMs).toBe(2500);
  });

  it("scales totalMs to the furthest reach and never divides by zero", () => {
    const empty = buildTimeline(run, []);
    expect(empty.totalMs).toBe(1);
    const { totalMs } = buildTimeline(run, [
      nodeRun({ node_key: "a", started_at: "2026-01-01T00:00:01.000Z", latency_ms: 4000 }),
    ]);
    expect(totalMs).toBe(5000);
  });
});
