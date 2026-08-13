"use client";

import type {
  DbConnection,
  DbConnectionInput,
  DbSchema,
  ProjectKind,
  ProjectSummary,
  WorkspaceProject,
} from "@workspace/api-client";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

export type InfraHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setDbConnections: Dispatch<SetStateAction<DbConnection[]>>;
  setProjects: Dispatch<SetStateAction<ProjectSummary[]>>;
  setActiveProject: Dispatch<SetStateAction<WorkspaceProject | null>>;
};

export function createInfraHandlers({
  setError,
  setDbConnections,
  setProjects,
  setActiveProject,
}: InfraHandlerDeps) {
  async function addDbConnection(input: DbConnectionInput) {
    setError("");
    try {
      const created = await api.createDbConnection(input);
      // A new connection is untested; dial it so the card shows a real status.
      const tested = await api.testDbConnection(created.id).catch(() => created);
      setDbConnections((items) => [...items, tested]);
      if (tested.status === "error") {
        setError(tested.last_error || "Could not reach that database");
      }
    } catch (caught) {
      setError(describeError(caught, "Could not add that connection"));
    }
  }

  async function testDbConnection(connectionId: string) {
    setError("");
    try {
      const tested = await api.testDbConnection(connectionId);
      setDbConnections((items) =>
        items.map((item) => (item.id === tested.id ? tested : item)),
      );
      if (tested.status === "error") {
        setError(tested.last_error || "Could not reach that database");
      }
    } catch (caught) {
      setError(describeError(caught, "Could not test that connection"));
    }
  }

  async function removeDbConnection(connection: DbConnection) {
    try {
      await api.deleteDbConnection(connection.id);
      setDbConnections((items) => items.filter((item) => item.id !== connection.id));
    } catch (caught) {
      setError(describeError(caught, "Could not remove that connection"));
    }
  }

  async function loadDbSchema(connectionId: string, table?: string): Promise<DbSchema> {
    return api.getDbSchema(connectionId, table);
  }

  async function openProject(projectId: string) {
    setError("");
    try {
      setActiveProject(await api.getProject(projectId));
    } catch (caught) {
      setError(describeError(caught, "Could not open that project"));
    }
  }

  async function createProject(
    name: string,
    description: string,
    kind: ProjectKind = "web",
  ) {
    setError("");
    try {
      const created = await api.createProject(name, description, kind);
      setActiveProject(created);
      setProjects(await api.listProjects());
    } catch (caught) {
      setError(describeError(caught, "Could not create that project"));
    }
  }

  async function saveProjectFile(projectId: string, path: string, content: string) {
    setError("");
    try {
      await api.saveProjectFile(projectId, path, content);
      // Re-read rather than patching locally: the server normalizes the path, so
      // a client-side guess at the stored path can miss (e.g. "./a//b.ts").
      setActiveProject(await api.getProject(projectId));
      setProjects(await api.listProjects());
    } catch (caught) {
      setError(describeError(caught, "Could not save that file"));
    }
  }

  async function setProjectEntry(projectId: string, path: string) {
    setError("");
    try {
      setActiveProject(await api.setProjectEntry(projectId, path));
      setProjects(await api.listProjects());
    } catch (caught) {
      setError(describeError(caught, "Could not make that the entry file"));
    }
  }

  async function removeProjectFile(projectId: string, path: string) {
    try {
      await api.deleteProjectFile(projectId, path);
      setActiveProject(await api.getProject(projectId));
      setProjects(await api.listProjects());
    } catch (caught) {
      setError(describeError(caught, "Could not delete that file"));
    }
  }

  async function removeProject(project: ProjectSummary) {
    try {
      await api.deleteProject(project.id);
      setActiveProject((current) => (current?.id === project.id ? null : current));
      setProjects(await api.listProjects());
    } catch (caught) {
      setError(describeError(caught, "Could not delete that project"));
    }
  }

  return {
    addDbConnection,
    testDbConnection,
    removeDbConnection,
    loadDbSchema,
    openProject,
    createProject,
    saveProjectFile,
    setProjectEntry,
    removeProjectFile,
    removeProject,
  };
}
