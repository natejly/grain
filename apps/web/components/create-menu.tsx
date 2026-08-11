"use client";

import type { DocumentKind } from "@workspace/api-client";
import { ArrowLeft, ChevronDown, Plus } from "lucide-react";
import { useState } from "react";
import { DisclosureMenu } from "./disclosure-menu";
import { CREATE_ACTIONS, type CreateAction } from "./views/navigation";
import { DOCUMENT_KIND_LABELS } from "./views/shared";

/**
 * "+ Create" — a button that makes things, not a place you visit.
 *
 * That distinction is the whole reason this exists. "Create" used to be a
 * sidebar destination, so picking it took you to a *list* of documents and left
 * the actual making to whatever button you could find once you got there.
 * Picking "Document" here creates a document and opens it.
 *
 * Two of the six need nothing said first — a sandbox is a machine, and a
 * dashboard collects its name, datasets and prompt in the editor it opens — so
 * they run on the first click. The rest ask for the one thing that cannot be
 * filled in later, since nothing here can be renamed after the fact.
 */
export type CreateMenuProps = {
  create: (action: CreateAction, name: string, kind: DocumentKind) => Promise<void>;
};

function CreatePanel({
  create,
  close,
}: CreateMenuProps & { close: () => void }) {
  // Held here rather than in CreateMenu on purpose: the panel unmounts when it
  // closes, so a half-filled form cannot be waiting on the next open.
  const [chosen, setChosen] = useState<CreateAction | null>(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<DocumentKind>("markdown");
  const [busy, setBusy] = useState(false);

  async function run(action: CreateAction, value: string) {
    if (busy) return;
    setBusy(true);
    try {
      await create(action, value, kind);
    } finally {
      setBusy(false);
    }
    close();
  }

  if (!chosen) {
    return (
      <>
        {CREATE_ACTIONS.map((action) => {
          const Icon = action.icon;
          return (
            <button
              key={action.id}
              className="disclosure-option"
              disabled={busy}
              onClick={() => {
                if (action.prompt) {
                  setChosen(action);
                  return;
                }
                void run(action, "");
              }}
            >
              <Icon size={14} />
              <span className="disclosure-option-name">{action.label}</span>
            </button>
          );
        })}
      </>
    );
  }

  return (
    <form
      className="create-form"
      aria-label={`New ${chosen.noun}`}
      onSubmit={(event) => {
        event.preventDefault();
        if (!name.trim()) return;
        void run(chosen, name.trim());
      }}
    >
      <div className="create-form-head">
        <button
          type="button"
          className="create-back"
          aria-label="Back to the create menu"
          onClick={() => setChosen(null)}
        >
          <ArrowLeft size={13} />
        </button>
        <strong>New {chosen.noun}</strong>
      </div>
      <input
        value={name}
        aria-label={chosen.prompt}
        placeholder={chosen.prompt}
        autoFocus
        onChange={(event) => setName(event.target.value)}
      />
      {chosen.id === "document" && (
        <select
          value={kind}
          aria-label="Document format"
          onChange={(event) => setKind(event.target.value as DocumentKind)}
        >
          {Object.entries(DOCUMENT_KIND_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      )}
      <button type="submit" className="primary-button" disabled={busy || !name.trim()}>
        {busy ? "Creating…" : `Create ${chosen.noun}`}
      </button>
    </form>
  );
}

export function CreateMenu({ create }: CreateMenuProps) {
  return (
    <DisclosureMenu
      id="create-menu"
      // The visible word is "Create", which on its own reads as a submit button
      // — several of the views have one. This says what it opens.
      triggerLabel="Create new"
      triggerClassName="chrome-button accent"
      trigger={
        <>
          <Plus size={15} />
          <span className="chrome-button-label">Create</span>
          <ChevronDown size={13} />
        </>
      }
      menuLabel="Create"
    >
      {(close) => <CreatePanel create={create} close={close} />}
    </DisclosureMenu>
  );
}
