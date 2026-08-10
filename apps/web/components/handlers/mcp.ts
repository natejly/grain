"use client";

import type { McpServer, McpServerInput } from "@workspace/api-client";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

export type McpHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setMcpServers: Dispatch<SetStateAction<McpServer[]>>;
};

export function createMcpHandlers({ setError, setMcpServers }: McpHandlerDeps) {
  async function addMcpServer(input: McpServerInput) {
    setError("");
    try {
      const created = await api.createMcpServer(input);
      // A new server has no tools until it connects, so discover immediately.
      const refreshed = await api.refreshMcpServer(created.id);
      setMcpServers((items) => [...items, refreshed]);
      if (refreshed.status === "error") {
        setError(refreshed.last_error || "Could not reach that MCP server");
      }
    } catch (caught) {
      setError(describeError(caught, "Could not add that MCP server"));
    }
  }

  function replaceServer(server: McpServer) {
    setMcpServers((items) =>
      items.map((item) => (item.id === server.id ? server : item)),
    );
  }

  async function refreshMcpServer(serverId: string) {
    setError("");
    try {
      const server = await api.refreshMcpServer(serverId);
      replaceServer(server);
      if (server.status === "error") {
        setError(server.last_error || "Could not reach that MCP server");
      }
    } catch (caught) {
      setError(describeError(caught, "Could not refresh that MCP server"));
    }
  }

  async function setMcpServerEnabled(serverId: string, enabled: boolean) {
    try {
      replaceServer(await api.setMcpServerEnabled(serverId, enabled));
    } catch (caught) {
      setError(describeError(caught, "Could not update that MCP server"));
    }
  }

  async function setMcpToolEnabled(toolId: string, enabled: boolean) {
    try {
      await api.setMcpToolEnabled(toolId, enabled);
      setMcpServers((items) =>
        items.map((server) => ({
          ...server,
          tools: server.tools.map((tool) =>
            tool.id === toolId ? { ...tool, enabled } : tool,
          ),
        })),
      );
    } catch (caught) {
      setError(describeError(caught, "Could not update that tool"));
    }
  }

  async function removeMcpServer(server: McpServer) {
    try {
      await api.deleteMcpServer(server.id);
      setMcpServers((items) => items.filter((item) => item.id !== server.id));
    } catch (caught) {
      setError(describeError(caught, "Could not remove that MCP server"));
    }
  }

  return {
    addMcpServer,
    refreshMcpServer,
    setMcpServerEnabled,
    setMcpToolEnabled,
    removeMcpServer,
  };
}
