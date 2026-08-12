"use client";

import type { SandboxTool, SandboxToolInput } from "@workspace/api-client";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

export type SandboxToolHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setSandboxTools: Dispatch<SetStateAction<SandboxTool[]>>;
};

export function createSandboxToolHandlers({
  setError,
  setSandboxTools,
}: SandboxToolHandlerDeps) {
  async function addSandboxTool(input: SandboxToolInput) {
    setError("");
    try {
      const created = await api.createSandboxTool(input);
      setSandboxTools((items) => [...items, created]);
    } catch (caught) {
      setError(describeError(caught, "Could not add that sandbox tool"));
    }
  }

  async function setSandboxToolEnabled(toolId: string, enabled: boolean) {
    try {
      const updated = await api.updateSandboxTool(toolId, { enabled });
      setSandboxTools((items) =>
        items.map((item) => (item.id === toolId ? updated : item)),
      );
    } catch (caught) {
      setError(describeError(caught, "Could not update that sandbox tool"));
    }
  }

  async function removeSandboxTool(tool: SandboxTool) {
    try {
      await api.deleteSandboxTool(tool.id);
      setSandboxTools((items) => items.filter((item) => item.id !== tool.id));
    } catch (caught) {
      setError(describeError(caught, "Could not remove that sandbox tool"));
    }
  }

  return {
    addSandboxTool,
    setSandboxToolEnabled,
    removeSandboxTool,
  };
}
