"use client";

import type { SandboxSecret, SandboxSecretInput } from "@workspace/api-client";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

export type SandboxSecretHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setSandboxSecrets: Dispatch<SetStateAction<SandboxSecret[]>>;
};

export function createSandboxSecretHandlers({
  setError,
  setSandboxSecrets,
}: SandboxSecretHandlerDeps) {
  async function addSandboxSecret(input: SandboxSecretInput) {
    setError("");
    try {
      const saved = await api.putSandboxSecret(input);
      // Upsert by name: replace an existing row of the same name, else append.
      setSandboxSecrets((items) => {
        const without = items.filter((item) => item.name !== saved.name);
        return [...without, saved].sort((a, b) => a.name.localeCompare(b.name));
      });
    } catch (caught) {
      setError(describeError(caught, "Could not save that secret"));
    }
  }

  async function removeSandboxSecret(secret: SandboxSecret) {
    try {
      await api.deleteSandboxSecret(secret.name);
      setSandboxSecrets((items) =>
        items.filter((item) => item.name !== secret.name),
      );
    } catch (caught) {
      setError(describeError(caught, "Could not remove that secret"));
    }
  }

  return {
    addSandboxSecret,
    removeSandboxSecret,
  };
}
