import { describe, expect, it } from "vitest";
import type { GraphEdge, GraphEntity } from "@workspace/api-client";
import { graphSignature } from "../components/graph-3d";

/**
 * What counts as "a different graph" to the 3D canvas.
 *
 * Building the scene means a new WebGL context, a new force simulation and a
 * camera reset to its framing shot, so the build effect is keyed on this
 * signature rather than on the identity of the `entities`/`edges` props. It has
 * to be: the page hands down array literals (`graph?.edges.filter(...)`), so
 * their identity is new on every render, and the graph page re-renders on the
 * 400ms poll behind a rebuild, on the refresh after a chat turn, and on the
 * click that selects a node. Keyed on identity, every one of those threw away a
 * settled layout and started the graph over from noise.
 *
 * So: equal content must sign the same, and every field the scene actually
 * draws from must change the signature when it changes.
 */
function entity(over: Partial<GraphEntity> = {}): GraphEntity {
  return {
    id: "e1",
    name: "Atlas Labs",
    entity_type: "organization",
    mention_count: 3,
    chunk_ids: [],
    source_ids: [],
    memory_ids: [],
    ...over,
  } as GraphEntity;
}

function edge(over: Partial<GraphEdge> = {}): GraphEdge {
  return {
    from_entity_id: "e1",
    to_entity_id: "e2",
    relation: "co_occurs",
    weight: 1,
    ...over,
  } as GraphEdge;
}

describe("the 3d graph's scene signature", () => {
  it("is unchanged by a re-render that rebuilds the arrays", () => {
    const before = graphSignature([entity(), entity({ id: "e2" })], [edge()]);
    // Fresh objects and fresh arrays, same graph — this is exactly what the
    // page produces on every render.
    const after = graphSignature([entity(), entity({ id: "e2" })], [edge()]);
    expect(after).toBe(before);
  });

  it("ignores the fields the canvas never draws", () => {
    // Provenance moves whenever a rebuild reshuffles chunks; none of it reaches
    // a sphere, a label or a line, so none of it is worth a teardown.
    const before = graphSignature([entity()], []);
    const after = graphSignature(
      [entity({ chunk_ids: ["c1"], source_ids: ["s1"], memory_ids: ["m1"] })],
      [],
    );
    expect(after).toBe(before);
  });

  it.each([
    ["a renamed entity", entity({ name: "Atlas Labs Ltd" })],
    ["a retyped entity", entity({ entity_type: "project" })],
    ["a new mention count", entity({ mention_count: 4 })],
    ["a different entity", entity({ id: "e9" })],
  ])("changes on %s", (_case, changed) => {
    expect(graphSignature([changed], [])).not.toBe(graphSignature([entity()], []));
  });

  it("changes when an edge is retyped, which is what makes it solid or dashed", () => {
    expect(graphSignature([entity()], [edge({ relation: "owned_by" })])).not.toBe(
      graphSignature([entity()], [edge()]),
    );
  });

  it("changes when an edge moves, and when one is added", () => {
    const base = graphSignature([entity()], [edge()]);
    expect(graphSignature([entity()], [edge({ to_entity_id: "e3" })])).not.toBe(base);
    expect(graphSignature([entity()], [edge(), edge({ to_entity_id: "e3" })])).not.toBe(base);
  });

  it("keeps the entity and edge halves apart", () => {
    // Without a marker between the two lists, text moving across the boundary
    // could sign the same for graphs that draw differently.
    expect(graphSignature([entity({ id: "e1" })], [])).not.toBe(graphSignature([], [edge()]));
  });
});
