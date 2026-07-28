import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkspaceApi } from "@workspace/api-client";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("WorkspaceApi", () => {
  it("adds an idempotency key to mutations", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "conversation-1",
          title: "New conversation",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );

    await new WorkspaceApi("http://example.test").createConversation();

    const headers = new Headers(fetchMock.mock.calls[0]?.[1]?.headers);
    expect(headers.get("Idempotency-Key")).toBeTruthy();
    expect(headers.get("Content-Type")).toBe("application/json");
  });

  it("reports API health from the /health endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "ok", database: "ok" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const health = await new WorkspaceApi("http://example.test").health();

    expect(fetchMock.mock.calls[0]?.[0]).toBe("http://example.test/health");
    expect(health.status).toBe("ok");
  });

  it("parses ordered resumable SSE events", async () => {
    const body = [
      "id: 3",
      "event: message.delta",
      'data: {"delta":"Hello "}',
      "",
      "id: 4",
      "event: run.completed",
      'data: {"status":"completed"}',
      "",
      "",
    ].join("\n");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(body, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    );
    const events = [];
    for await (const event of new WorkspaceApi("http://example.test").streamRun(
      "run-1",
      2,
    )) {
      events.push(event);
    }
    expect(events.map((event) => event.id)).toEqual([3, 4]);
    expect(events[0]?.data.delta).toBe("Hello ");
  });
});

