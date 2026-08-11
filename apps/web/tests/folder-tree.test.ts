import { describe, expect, it } from "vitest";
import type { DocumentSummary, Folder } from "@workspace/api-client";
import {
  ROOT,
  buildTree,
  contentsLabel,
  folderPath,
  moveTargets,
  subtreeIds,
} from "../components/views/folder-tree";

/**
 * The server sends two flat lists; the sidebar shows a tree. Everything that
 * can go wrong between those two facts is in here — a file whose folder was
 * deleted out from under it, a move menu offering a destination the server will
 * refuse, a cycle that would render until the stack ran out.
 */
function folder(id: string, name: string, parent = ROOT): Folder {
  return { id, name, parent_id: parent, updated_at: "2026-01-01T00:00:00Z" };
}

function file(id: string, title: string, folderId = ROOT): DocumentSummary {
  return {
    id,
    title,
    kind: "markdown",
    characters: 10,
    folder_id: folderId,
    updated_at: "2026-01-01T00:00:00Z",
  };
}

describe("building the tree", () => {
  it("nests folders under their parents and files under their folders", () => {
    const tree = buildTree(
      [folder("a", "Research"), folder("b", "Interviews", "a")],
      [file("f1", "Notes", "b"), file("f2", "Loose")],
    );
    expect(tree.folders.map((node) => node.folder.id)).toEqual(["a"]);
    expect(tree.folders[0].folders[0].files.map((doc) => doc.id)).toEqual(["f1"]);
    expect(tree.files.map((doc) => doc.id)).toEqual(["f2"]);
  });

  it("sorts siblings by name at every level", () => {
    const tree = buildTree(
      [folder("a", "Zebra"), folder("b", "Apple"), folder("c", "Mango", "b")],
      [],
    );
    expect(tree.folders.map((node) => node.folder.name)).toEqual(["Apple", "Zebra"]);
    expect(tree.folders[0].folders.map((node) => node.folder.name)).toEqual(["Mango"]);
  });

  it("surfaces a file whose folder no longer exists rather than losing it", () => {
    // The failure this exists to prevent: a document the API lists, that the
    // user wrote, appearing nowhere on screen because one join missed.
    const tree = buildTree([], [file("f1", "Orphan", "deleted-folder")]);
    expect(tree.files.map((doc) => doc.title)).toEqual(["Orphan"]);
  });

  it("re-roots a folder whose parent is missing", () => {
    const tree = buildTree([folder("a", "Research", "gone")], []);
    expect(tree.folders.map((node) => node.folder.id)).toEqual(["a"]);
    expect(tree.folders[0].depth).toBe(1);
  });

  it("breaks a cycle rather than recursing forever or rendering nothing", () => {
    // Not reachable through the API — the server refuses the move — but a tree
    // walk that trusts its input is one bad row away from a blank page, and a
    // walk that merely *terminates* on it drops both folders and everything
    // filed inside them.
    const tree = buildTree(
      [folder("a", "A", "b"), folder("b", "B", "a")],
      [file("f1", "Inside", "b")],
    );
    const rendered: string[] = [];
    const walk = (nodes: typeof tree.folders) => {
      for (const node of nodes) {
        rendered.push(node.folder.id);
        walk(node.folders);
      }
    };
    walk(tree.folders);
    expect(rendered.sort()).toEqual(["a", "b"]);
    expect(tree.folders[0].total).toBe(1);
  });

  it("counts files at and below a folder, which is what blocks its delete", () => {
    const tree = buildTree(
      [folder("a", "Research"), folder("b", "Interviews", "a")],
      [file("f1", "One", "a"), file("f2", "Two", "b"), file("f3", "Three", "b")],
    );
    expect(tree.folders[0].total).toBe(3);
    expect(tree.folders[0].folders[0].total).toBe(2);
    expect(contentsLabel(tree.folders[0])).toBe("3 files");
    expect(contentsLabel(tree.folders[0].folders[0])).toBe("2 files");
  });

  it("says nothing about an empty folder, and counts one file singular", () => {
    const tree = buildTree(
      [folder("a", "Empty"), folder("b", "One")],
      [file("f1", "Only", "b")],
    );
    expect(contentsLabel(tree.folders[0])).toBe("");
    expect(contentsLabel(tree.folders[1])).toBe("1 file");
  });

  it("gives each level a depth, starting at one", () => {
    const tree = buildTree(
      [folder("a", "A"), folder("b", "B", "a"), folder("c", "C", "b")],
      [],
    );
    expect(tree.folders[0].depth).toBe(1);
    expect(tree.folders[0].folders[0].depth).toBe(2);
    expect(tree.folders[0].folders[0].folders[0].depth).toBe(3);
  });
});

describe("where a thing may be moved", () => {
  const tree = [
    folder("a", "Research"),
    folder("b", "Interviews", "a"),
    folder("c", "2025", "b"),
    folder("d", "Archive"),
  ];

  it("never offers a folder its own subtree", () => {
    // The menu must not list a move the server will refuse; a menu whose
    // options fail is a menu users stop reading.
    const targets = moveTargets(tree, "a", ROOT).map((target) => target.id);
    expect(targets).not.toContain("a");
    expect(targets).not.toContain("b");
    expect(targets).not.toContain("c");
    expect(targets).toContain("d");
  });

  it("never offers the parent a thing already has", () => {
    expect(moveTargets(tree, "c", "b").map((target) => target.id)).not.toContain("b");
  });

  it("offers the top level only to something that is not already there", () => {
    expect(moveTargets(tree, "b", "a").map((target) => target.label)).toContain(
      "Top level",
    );
    expect(moveTargets(tree, "a", ROOT).map((target) => target.label)).not.toContain(
      "Top level",
    );
  });

  it("names destinations by full path, since two folders can share a name", () => {
    const twins = [folder("a", "2025"), folder("b", "Research"), folder("c", "2025", "b")];
    // "Top level" leads, because it is the only destination that is not a row
    // in the tree; the folders that follow are in path order.
    expect(moveTargets(twins, "", "a").map((target) => target.label)).toEqual([
      "Top level",
      "Research",
      "Research / 2025",
    ]);
  });

  it("lets a file go anywhere but where it is", () => {
    // A file has no subtree, so only its current folder is excluded.
    expect(moveTargets(tree, "", "b").map((target) => target.id).sort()).toEqual(
      ["", "a", "c", "d"].sort(),
    );
  });

  it("offers nothing when there are no folders at all", () => {
    expect(moveTargets([], "", ROOT)).toEqual([]);
  });
});

describe("naming a folder", () => {
  const tree = [folder("a", "Research"), folder("b", "Interviews", "a")];

  it("reads from the top down", () => {
    expect(folderPath(tree, "b")).toBe("Research / Interviews");
    expect(folderPath(tree, "a")).toBe("Research");
  });

  it("is empty for the top level and for a folder we were not given", () => {
    expect(folderPath(tree, ROOT)).toBe("");
    expect(folderPath(tree, "gone")).toBe("");
  });

  it("collects the whole subtree, itself included", () => {
    expect(subtreeIds(tree, "a").sort()).toEqual(["a", "b"]);
    expect(subtreeIds(tree, "b")).toEqual(["b"]);
  });
});
