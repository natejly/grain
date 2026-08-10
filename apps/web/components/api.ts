import { WorkspaceApi } from "@workspace/api-client";

/** One client for the whole workspace: module state, not view or hook state. */
export const api = new WorkspaceApi(
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000",
);
