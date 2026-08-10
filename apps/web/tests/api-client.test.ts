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

  it("lists the caller's workspaces and sends the selection on later requests", async () => {
    // A fresh Response per call: a body can only be read once.
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(
        new Response(
          JSON.stringify([
            { id: "ws-1", name: "Acme Knowledge Lab", role: "owner", is_current: true },
            { id: "ws-2", name: "Field notes", role: "member", is_current: false },
          ]),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    const api = new WorkspaceApi("http://example.test");

    const workspaces = await api.listWorkspaces();
    expect(fetchMock.mock.calls[0]?.[0]).toBe("http://example.test/api/auth/workspaces");
    expect(workspaces.map((item) => item.id)).toEqual(["ws-1", "ws-2"]);

    // Selecting one is what makes the switcher work: everything after it must
    // carry the header the API checks against memberships.
    api.setWorkspaceId("ws-2");
    await api.listSources();
    const headers = new Headers(fetchMock.mock.calls[1]?.[1]?.headers);
    expect(headers.get("X-Workspace-Id")).toBe("ws-2");
  });

  it("turns an unreachable API into a typed offline error", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new TypeError("Failed to fetch"),
    );
    const api = new WorkspaceApi("http://example.test");

    await expect(api.listSources()).rejects.toMatchObject({
      status: 0,
      offline: true,
    });
    await expect(api.listSources()).rejects.toThrow(
      "Cannot reach the API at http://example.test",
    );
  });

  it("sends the CSRF header on unsafe methods only, once a session is adopted", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) =>
      Promise.resolve(
        String(input).endsWith("/api/auth/me")
          ? new Response(JSON.stringify({ csrf_token: "tok-1", workspace_id: "ws-1" }), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            })
          : new Response("[]", {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
      ),
    );
    const api = new WorkspaceApi("http://example.test");

    await api.listSources();
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get("X-CSRF-Token")).toBe(
      null,
    );

    await api.me();
    await api.listSources();
    await api.deleteSource("source-1");

    const get = new Headers(fetchMock.mock.calls[2]?.[1]?.headers);
    const del = new Headers(fetchMock.mock.calls[3]?.[1]?.headers);
    expect(get.get("X-CSRF-Token")).toBe(null);
    expect(get.get("X-Workspace-Id")).toBe("ws-1");
    expect(del.get("X-CSRF-Token")).toBe("tok-1");
  });

  it("re-reads a rotated CSRF token and retries the rejected write once", async () => {
    let rejections = 1;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (String(input).endsWith("/api/auth/me")) {
        return Promise.resolve(
          new Response(JSON.stringify({ csrf_token: "fresh", workspace_id: "" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      if (rejections-- > 0) {
        return Promise.resolve(
          new Response(JSON.stringify({ detail: "CSRF token missing or invalid" }), {
            status: 403,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.resolve(new Response(null, { status: 204 }));
    });

    await new WorkspaceApi("http://example.test").deleteSource("source-1");

    const paths = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(paths).toEqual([
      "http://example.test/api/sources/source-1",
      "http://example.test/api/auth/me",
      "http://example.test/api/sources/source-1",
    ]);
    // The retry is the same operation, so it must reuse the idempotency key.
    const first = new Headers(fetchMock.mock.calls[0]?.[1]?.headers);
    const retry = new Headers(fetchMock.mock.calls[2]?.[1]?.headers);
    expect(retry.get("X-CSRF-Token")).toBe("fresh");
    expect(retry.get("Idempotency-Key")).toBe(first.get("Idempotency-Key"));
  });

  it("announces a 401 once, and never for a rejected login", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid email or password" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const api = new WorkspaceApi("http://example.test");
    const signedOut = vi.fn();
    api.onUnauthorized(signedOut);

    await expect(api.login("a@b.co", "nope")).rejects.toMatchObject({ status: 401 });
    expect(signedOut).not.toHaveBeenCalled();

    await expect(api.listSources()).rejects.toMatchObject({ status: 401 });
    expect(signedOut).toHaveBeenCalledTimes(1);
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

