import { describe, expect, it } from "vitest";
import { ApiError } from "@workspace/api-client";
import { describeActionError, describeError } from "../components/views/shared";

/**
 * The two error describers differ in exactly one case, and that case is the
 * whole reason the second one exists.
 *
 * `describeError` says nothing when the API could not be reached, because
 * during a full outage every background refresh fails at once and the health
 * banner explains it better than a stack of identical toasts would. Applied to
 * a turn somebody just sent, that silence made the primary action of the app
 * fail invisibly: the words stayed in the composer, no message appeared, and
 * nothing on screen distinguished "the send failed" from "the keystroke was
 * ignored". Status 0 is not only the outage the banner covers — it is also a
 * single blipped request, and a 500 that unwinds past the CORS middleware and
 * so reaches the browser stripped of its status.
 *
 * These tests pin the split in both directions: the background describer must
 * keep quiet, and the action describer must never be empty.
 */

const offline = () => new ApiError("Failed to fetch", 0);

describe("describeError", () => {
  it("stays silent for an unreachable API, leaving the banner to say it", () => {
    expect(describeError(offline(), "Could not send message")).toBe("");
  });

  it("prefers the API's own words when it answered", () => {
    expect(describeError(new ApiError("Agent is not available", 422), "fallback")).toBe(
      "Agent is not available",
    );
  });

  it("falls back when the failure carries no message", () => {
    expect(describeError({}, "Could not send message")).toBe("Could not send message");
  });
});

describe("describeActionError", () => {
  it("speaks up for an unreachable API rather than failing silently", () => {
    const message = describeActionError(offline(), "Could not send message");
    expect(message).not.toBe("");
    expect(message).toContain("Could not send message");
    expect(message).toContain("could not be reached");
  });

  it("names the caller's own action, so one sentence serves every call site", () => {
    expect(describeActionError(offline(), "Could not regenerate")).toContain(
      "Could not regenerate",
    );
    expect(describeActionError(offline(), "Could not stop the run")).toContain(
      "Could not stop the run",
    );
  });

  it("is identical to describeError whenever the API actually answered", () => {
    for (const caught of [
      new ApiError("Agent is not available", 422),
      new ApiError("Conversation not found", 404),
      {},
    ]) {
      expect(describeActionError(caught, "Could not send message")).toBe(
        describeError(caught, "Could not send message"),
      );
    }
  });
});
