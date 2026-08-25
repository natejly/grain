import { cleanup, render, screen } from "@testing-library/react";
import { act, createElement } from "react";
import { afterEach, describe, expect, it } from "vitest";
import type { Bootstrap } from "@workspace/api-client";
import { SystemStatus } from "../components/system-status";

/**
 * The system-status popover replaced two always-on pills, so the facts they
 * carried have to survive the move: the model and its provider, the screen's
 * posture (only when it is on), the API's reachability, and — the one that
 * must never soften — the development bypass named out loud. Asserted on the
 * rendered text, same as the unrestricted-indicator suite: a panel that opens
 * with the wrong words in it is the same failure wearing a passing test.
 */
function bootstrap(overrides: Partial<Bootstrap> = {}): Bootstrap {
  return {
    identity: {
      user_id: "user-1",
      user_name: "Nate",
      workspace_id: "ws-1",
      workspace_name: "Grain",
      role: "owner",
    },
    default_agent_id: "",
    feature_flags: {},
    model_provider: {
      provider: "openai",
      configured: true,
      model: "gpt-5.2",
      selectable_models: [],
      reasoning_efforts: [],
      default_effort: "medium",
    },
    screen: { enabled: true, mode: "enforce", backend: "proxy" },
    unrestricted_agent: false,
    ...overrides,
  };
}

function open(props: { bootstrap: Bootstrap | null; apiDown: boolean }) {
  render(createElement(SystemStatus, props));
  act(() => {
    screen.getByRole("button", { name: "System status" }).click();
  });
}

afterEach(cleanup);

describe("the system-status popover", () => {
  it("has a named trigger before it is opened", () => {
    render(createElement(SystemStatus, { bootstrap: bootstrap(), apiDown: false }));
    expect(screen.getByRole("button", { name: "System status" })).toBeTruthy();
  });

  it("lists the model, the screen's posture and a reachable API", () => {
    open({ bootstrap: bootstrap(), apiDown: false });
    expect(screen.getByText(/gpt-5\.2/)).toBeTruthy();
    expect(screen.getByText(/enforce mode · proxy backend/)).toBeTruthy();
    expect(screen.getByText("Reachable")).toBeTruthy();
    expect(screen.queryByText(/DEV_UNRESTRICTED_AGENT/)).toBeNull();
  });

  it("omits the screen row when the screen is off", () => {
    open({
      bootstrap: bootstrap({
        screen: { enabled: false, mode: "shadow", backend: "builtin" },
      }),
      apiDown: false,
    });
    expect(screen.queryByText(/Prompt-injection screen/)).toBeNull();
  });

  it("names the dev bypass when the deployment is unrestricted", () => {
    open({ bootstrap: bootstrap({ unrestricted_agent: true }), apiDown: false });
    expect(
      screen.getByText(/DEV_UNRESTRICTED_AGENT is on: every tool runs/),
    ).toBeTruthy();
  });

  it("says the API is unreachable while it is", () => {
    open({ bootstrap: bootstrap(), apiDown: true });
    expect(screen.getByText("Unreachable — retrying")).toBeTruthy();
  });
});
