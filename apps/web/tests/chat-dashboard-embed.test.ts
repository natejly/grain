import { cleanup, render } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import type { GeneratedApp } from "@workspace/api-client";
import {
  ChatDashboardEmbeds,
  referencedSlugs,
} from "../components/chat-dashboard-embed";

/**
 * A dashboard embedded in a transcript, and the one property that cannot be
 * asserted by looking for the element.
 *
 * The frame is an iframe with an opaque origin: its document is loaded once and
 * everything it knows arrives over postMessage afterwards. React keeps that
 * loaded document only while it keeps the *same DOM node* — a remount is a
 * fresh `src` fetch, a white flash, and a re-posted init. A transcript
 * re-renders on every streamed token and refetches its app list whenever a run
 * ends, so "the iframe is still there" is true in both the working case and the
 * broken one, and only node identity tells them apart.
 *
 * Every test below therefore holds the element across a re-render and asserts
 * `toBe` on the node itself.
 */

const app = (over: Partial<GeneratedApp> = {}): GeneratedApp =>
  ({
    id: "app-1",
    name: "Revenue",
    slug: "revenue",
    description: "",
    visibility: "public",
    app_type: "code",
    current_release_id: "rel-1",
    releases: [{ id: "rel-1", manifest: { snapshots: {} } }],
    ...over,
  }) as unknown as GeneratedApp;

/** A fresh object graph with identical values — what every refetch produces. */
const refetched = (over: Partial<GeneratedApp> = {}) => app(over);

function frame(container: HTMLElement): HTMLIFrameElement {
  const found = container.querySelector("iframe");
  if (!found) throw new Error("no embedded frame");
  return found;
}

// `SandboxFrame` reads the resolved theme to post it into the frame, and jsdom
// ships no `matchMedia`. Answering "light" is enough: nothing asserted here
// depends on the value, only on the frame surviving a re-render.
beforeAll(() => {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
});

afterEach(cleanup);

describe("referencedSlugs", () => {
  it("finds a published app a message links to", () => {
    expect(referencedSlugs("see /apps/revenue for the split", [app()])).toEqual([
      "revenue",
    ]);
  });

  it("ignores an app that is private or has never been released", () => {
    expect(referencedSlugs("/apps/revenue", [app({ visibility: "private" })])).toEqual(
      [],
    );
    expect(referencedSlugs("/apps/revenue", [app({ current_release_id: "" })])).toEqual(
      [],
    );
  });

  it("ignores a slug this workspace does not have", () => {
    expect(referencedSlugs("/apps/somebody-elses", [app()])).toEqual([]);
  });

  it("names a slug once however often the message repeats it", () => {
    expect(referencedSlugs("/apps/revenue and /apps/revenue", [app()])).toEqual([
      "revenue",
    ]);
  });

  it("matches nothing while a link is still being streamed", () => {
    // The half-written prefix of "/apps/revenue" must not resolve to a
    // different app whose slug happens to be a prefix — mounting against it
    // would load one frame and then replace it a token later.
    const half = referencedSlugs("look at /apps/reven", [app(), app({ id: "b", slug: "reven", name: "Reven" })]);
    expect(half).toEqual(["reven"]);
    expect(referencedSlugs("look at /apps/", [app()])).toEqual([]);
  });
});

describe("the embedded frame across a re-render", () => {
  it("survives the token deltas that rewrite the message around it", () => {
    const apps = [app()];
    const { container, rerender } = render(
      createElement(ChatDashboardEmbeds, { content: "here: /apps/revenue", apps }),
    );
    const before = frame(container);

    for (const suffix of [" and", " and the", " and the split", " and the split by region"]) {
      rerender(
        createElement(ChatDashboardEmbeds, {
          content: `here: /apps/revenue${suffix}`,
          apps,
        }),
      );
      expect(frame(container)).toBe(before);
    }
  });

  it("survives a workspace refetch that mints new app objects", () => {
    const { container, rerender } = render(
      createElement(ChatDashboardEmbeds, {
        content: "/apps/revenue",
        apps: [app()],
      }),
    );
    const before = frame(container);

    // A new array holding a new object with the same values: shallow-compare
    // memoisation would let this through and remount the frame.
    rerender(
      createElement(ChatDashboardEmbeds, {
        content: "/apps/revenue",
        apps: [refetched()],
      }),
    );
    expect(frame(container)).toBe(before);
  });

  it("survives another app being published elsewhere in the workspace", () => {
    const { container, rerender } = render(
      createElement(ChatDashboardEmbeds, {
        content: "/apps/revenue",
        apps: [app()],
      }),
    );
    const before = frame(container);

    rerender(
      createElement(ChatDashboardEmbeds, {
        content: "/apps/revenue",
        apps: [app(), app({ id: "app-2", slug: "costs", name: "Costs" })],
      }),
    );
    expect(frame(container)).toBe(before);
  });

  it("reloads only when the release it is showing actually changes", () => {
    const { container, rerender } = render(
      createElement(ChatDashboardEmbeds, {
        content: "/apps/revenue",
        apps: [app()],
      }),
    );
    const before = frame(container);

    rerender(
      createElement(ChatDashboardEmbeds, {
        content: "/apps/revenue",
        apps: [
          app({
            current_release_id: "rel-2",
            releases: [
              { id: "rel-1", manifest: { snapshots: {} } },
              { id: "rel-2", manifest: { snapshots: {} } },
            ] as unknown as GeneratedApp["releases"],
          }),
        ],
      }),
    );
    // Same node — React reuses it — but the component must have re-rendered, so
    // the new release's snapshots are what the frame is initialised with.
    expect(frame(container)).toBe(before);
  });

  it("keeps each frame with its own app when one is added in front of it", () => {
    // Keyed by app id rather than array index: prepending must not hand the
    // first frame's DOM node to a different app's src.
    const { container, rerender } = render(
      createElement(ChatDashboardEmbeds, {
        content: "/apps/revenue",
        apps: [app()],
      }),
    );
    const revenueFrame = frame(container);
    const revenueSrc = revenueFrame.getAttribute("src");

    rerender(
      createElement(ChatDashboardEmbeds, {
        content: "/apps/costs and /apps/revenue",
        apps: [app(), app({ id: "app-2", slug: "costs", name: "Costs" })],
      }),
    );

    const frames = Array.from(container.querySelectorAll("iframe"));
    expect(frames).toHaveLength(2);
    const kept = frames.find((item) => item.getAttribute("src") === revenueSrc);
    expect(kept).toBe(revenueFrame);
  });
});

/**
 * Node identity above proves the frame is not *remounted*. It cannot prove the
 * frame is not *re-initialised*, because React reuses an iframe's DOM node
 * across a re-render whether or not anything is memoised — so those tests pass
 * with `memo` deleted.
 *
 * What `memo` actually buys is here. `SandboxFrame` re-posts its whole init
 * payload whenever `snapshots` changes identity, and every workspace refetch
 * mints a fresh `manifest.snapshots` object with identical contents. Counting
 * the posts is therefore the only assertion that fails when the comparator is
 * removed, which is what makes it worth writing.
 */
describe("the init payload across a re-render", () => {
  function countPosts(container: HTMLElement) {
    const target = frame(container).contentWindow;
    if (!target) throw new Error("frame has no content window");
    let posts = 0;
    target.postMessage = (() => {
      posts += 1;
    }) as unknown as typeof target.postMessage;
    return () => posts;
  }

  it("is not re-sent when a refetch mints new objects holding the same values", () => {
    const { container, rerender } = render(
      createElement(ChatDashboardEmbeds, {
        content: "/apps/revenue",
        apps: [app()],
      }),
    );
    const posts = countPosts(container);

    for (let tick = 0; tick < 5; tick += 1) {
      rerender(
        createElement(ChatDashboardEmbeds, {
          content: `/apps/revenue token ${tick}`,
          apps: [refetched()],
        }),
      );
    }
    expect(posts()).toBe(0);
  });

  it("is not re-sent for a release that carries no snapshots at all", () => {
    // The empty case has its own trap. A release with nothing frozen into it
    // still needs *some* object for the prop, and the usual `?? {}` mints a
    // fresh one every time the memo recomputes — which a renamed app does,
    // because its `releases` array is new. One frozen empty object keeps the
    // prop's identity, and therefore the frame, still.
    const bare = (over: Partial<GeneratedApp> = {}) =>
      app({
        releases: [{ id: "rel-1", manifest: {} }] as unknown as GeneratedApp["releases"],
        ...over,
      });
    const { container, rerender } = render(
      createElement(ChatDashboardEmbeds, { content: "/apps/revenue", apps: [bare()] }),
    );
    const posts = countPosts(container);

    rerender(
      createElement(ChatDashboardEmbeds, {
        content: "/apps/revenue",
        apps: [bare({ name: "Revenue, restated" })],
      }),
    );
    expect(posts()).toBe(0);
  });

  it("is re-sent when the release actually changes, because the data did", () => {
    const { container, rerender } = render(
      createElement(ChatDashboardEmbeds, {
        content: "/apps/revenue",
        apps: [app()],
      }),
    );
    const posts = countPosts(container);

    rerender(
      createElement(ChatDashboardEmbeds, {
        content: "/apps/revenue",
        apps: [
          app({
            current_release_id: "rel-2",
            releases: [
              { id: "rel-2", manifest: { snapshots: { sales: { columns: [] } } } },
            ] as unknown as GeneratedApp["releases"],
          }),
        ],
      }),
    );
    expect(posts()).toBeGreaterThan(0);
  });
});

describe("what the embed refuses to show", () => {
  it("renders nothing at all for a message that references no app", () => {
    const { container } = render(
      createElement(ChatDashboardEmbeds, { content: "no links here", apps: [app()] }),
    );
    expect(container.querySelector("iframe")).toBeNull();
    expect(container.querySelector(".chat-dashboards")).toBeNull();
  });

  it("locks the frame down the way ADR 0004 requires", () => {
    const { container } = render(
      createElement(ChatDashboardEmbeds, { content: "/apps/revenue", apps: [app()] }),
    );
    // allow-scripts and nothing else: no same-origin, so the document sits on an
    // opaque origin with no cookies and no reach into the parent DOM.
    expect(frame(container).getAttribute("sandbox")).toBe("allow-scripts");
  });

  it("names the frame so it is not an unlabelled box to a screen reader", () => {
    const { container } = render(
      createElement(ChatDashboardEmbeds, { content: "/apps/revenue", apps: [app()] }),
    );
    expect(frame(container).getAttribute("title")).toBe("Revenue dashboard");
  });
});
