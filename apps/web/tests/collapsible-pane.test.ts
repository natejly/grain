import { afterEach, describe, expect, it } from "vitest";
import {
  collapseKey,
  collapseLabel,
  persistCollapsed,
  persistSectionCollapsed,
  readCollapsed,
  readSectionCollapsed,
  sectionCollapseKey,
} from "../components/collapsible-pane";

afterEach(() => window.localStorage.clear());

describe("collapseKey", () => {
  it("namespaces per pane, so two panes cannot overwrite each other", () => {
    expect(collapseKey("rail")).toBe("grain.collapsed.rail");
    expect(collapseKey("documents-list")).toBe("grain.collapsed.documents-list");
    expect(collapseKey("rail")).not.toBe(collapseKey("documents-list"));
  });
});

describe("readCollapsed / persistCollapsed", () => {
  it("round-trips one pane without disturbing another", () => {
    persistCollapsed("rail", true);
    expect(readCollapsed("rail")).toBe(true);
    expect(readCollapsed("documents-list")).toBe(false);

    persistCollapsed("documents-list", true);
    expect(readCollapsed("rail")).toBe(true);
    expect(readCollapsed("documents-list")).toBe(true);

    persistCollapsed("rail", false);
    expect(readCollapsed("rail")).toBe(false);
    expect(readCollapsed("documents-list")).toBe(true);
  });

  it("expands rather than throws on a value nothing here wrote", () => {
    // The store is one hand-edit away from anything; the pane a user cannot see
    // is the worse failure, so an unrecognised value means "showing".
    for (const value of ["true", "yes", "0", "", "{}"]) {
      window.localStorage.setItem(collapseKey("rail"), value);
      expect(readCollapsed("rail")).toBe(false);
    }
  });

  it("leaves nothing behind when a pane is expanded again", () => {
    persistCollapsed("rail", true);
    persistCollapsed("rail", false);
    expect(window.localStorage.getItem(collapseKey("rail"))).toBeNull();
  });
});

describe("sidebar sections", () => {
  it("keys per group and section, apart from the panes", () => {
    // The shelves inside a destination's sidebar remember their folds under
    // their own family — a section named like a pane must not collide with it.
    expect(sectionCollapseKey("files", "data")).toBe("grain.section.files.data");
    expect(sectionCollapseKey("files", "rail")).not.toBe(collapseKey("rail"));
  });

  it("round-trips one shelf without disturbing its neighbours, expanded by default", () => {
    expect(readSectionCollapsed("files", "knowledge")).toBe(false);
    persistSectionCollapsed("files", "knowledge", true);
    expect(readSectionCollapsed("files", "knowledge")).toBe(true);
    expect(readSectionCollapsed("files", "data")).toBe(false);
    // Same total parse as the panes: anything but the stored "1" is showing.
    window.localStorage.setItem(sectionCollapseKey("files", "data"), "yes");
    expect(readSectionCollapsed("files", "data")).toBe(false);
    // Expanding again leaves nothing behind.
    persistSectionCollapsed("files", "knowledge", false);
    expect(window.localStorage.getItem(sectionCollapseKey("files", "knowledge"))).toBeNull();
  });
});

describe("collapseLabel", () => {
  it("names the state pressing it puts you in, not the pane's current one", () => {
    // The trap this exists to avoid: a button called "Hide the sidebar" that is
    // the only way to bring the sidebar back.
    expect(collapseLabel("sidebar", false)).toBe("Hide the sidebar");
    expect(collapseLabel("sidebar", true)).toBe("Show the sidebar");
    expect(collapseLabel("file list", true)).toBe("Show the file list");
    expect(collapseLabel("project list", false)).toBe("Hide the project list");
  });
});
