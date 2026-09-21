"use client";

import { KeyRound, UserRound } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { describeActionError } from "./shared";

/**
 * The person, not the workspace: display name and password.
 *
 * The email row is read-only — it is the login identity, and changing it is a
 * verification flow this pane does not own. The rename edits `users.name` in
 * place (already the attribution source everywhere), so live surfaces pick it
 * up on their next read while labels stamped into past events keep the old
 * name: records are records, and the hint under the field says so.
 */
export type ProfileViewProps = {
  /** From useSession — AuthSession already carries both. */
  userEmail: string;
  userName: string;
  /**
   * Called with the server-confirmed name after a successful rename, so the
   * shell can refresh the session provider's cached copy (presence chips and
   * attribution then show the new name without a reload).
   */
  onRenamed?: (name: string) => void;
};

export function ProfileView({ userEmail, userName, onRenamed }: ProfileViewProps) {
  const [name, setName] = useState(userName);
  const [nameBusy, setNameBusy] = useState(false);
  const [nameError, setNameError] = useState("");
  const [nameSaved, setNameSaved] = useState(false);

  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [passwordError, setPasswordError] = useState("");
  const [passwordSaved, setPasswordSaved] = useState("");

  const saveName = async () => {
    const trimmed = name.trim();
    if (!trimmed || nameBusy) return;
    setNameBusy(true);
    setNameError("");
    setNameSaved(false);
    try {
      const saved = await api.updateProfile(trimmed);
      setName(saved.name);
      setNameSaved(true);
      onRenamed?.(saved.name);
    } catch (caught) {
      setNameError(describeActionError(caught, "The name was not saved"));
    } finally {
      setNameBusy(false);
    }
  };

  const savePassword = async () => {
    if (passwordBusy) return;
    setPasswordError("");
    setPasswordSaved("");
    // The one check the client owns: the two typings of the NEW password
    // must match before anything goes over the wire.
    if (next !== confirm) {
      setPasswordError("The new passwords do not match");
      return;
    }
    if (!current || !next) {
      setPasswordError("Both the current and the new password are required");
      return;
    }
    setPasswordBusy(true);
    try {
      const acknowledged = await api.changePassword(current, next);
      setCurrent("");
      setNext("");
      setConfirm("");
      setPasswordSaved(acknowledged.detail || "Password updated.");
    } catch (caught) {
      // The server's refusals carry their own words: the generic 403, the
      // policy 422, the federated-account 422.
      setPasswordError(
        describeActionError(caught, "The password was not changed"),
      );
    } finally {
      setPasswordBusy(false);
    }
  };

  return (
    <section className="content-page profile-page">
      <div className="page-heading">
        <div>
          <h1>Profile</h1>
          <p>Who you are here: your name to teammates, and your password.</p>
        </div>
      </div>

      <div className="profile-panel">
        <div className="panel-title">
          <div>
            <UserRound size={14} />
            <strong>Identity</strong>
          </div>
        </div>
        <div className="profile-field">
          <label htmlFor="profile-email">Email</label>
          {/* Read-only on purpose: the login identity is not edited here. */}
          <input id="profile-email" type="email" value={userEmail} readOnly />
        </div>
        <div className="profile-field">
          <label htmlFor="profile-name">Display name</label>
          <input
            id="profile-name"
            type="text"
            value={name}
            maxLength={120}
            onChange={(event) => {
              setName(event.target.value);
              setNameSaved(false);
            }}
          />
        </div>
        <p className="profile-hint">
          Shown to teammates on messages, presence and new activity. Entries
          already in the audit trail keep the name they were recorded under.
        </p>
        {nameError && (
          <p className="profile-error" role="alert">
            {nameError}
          </p>
        )}
        {nameSaved && (
          <p className="profile-success" role="status">
            Name saved.
          </p>
        )}
        <div className="profile-actions">
          <button
            className="ghost-button"
            disabled={!name.trim() || name.trim() === userName || nameBusy}
            onClick={() => void saveName()}
          >
            {nameBusy ? "Saving…" : "Save name"}
          </button>
        </div>
      </div>

      <div className="profile-panel">
        <div className="panel-title">
          <div>
            <KeyRound size={14} />
            <strong>Password</strong>
          </div>
        </div>
        <div className="profile-field">
          <label htmlFor="profile-current-password">Current password</label>
          <input
            id="profile-current-password"
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(event) => setCurrent(event.target.value)}
          />
        </div>
        <div className="profile-field">
          <label htmlFor="profile-new-password">New password</label>
          <input
            id="profile-new-password"
            type="password"
            autoComplete="new-password"
            value={next}
            onChange={(event) => setNext(event.target.value)}
          />
        </div>
        <div className="profile-field">
          <label htmlFor="profile-confirm-password">Confirm new password</label>
          <input
            id="profile-confirm-password"
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
          />
        </div>
        <p className="profile-hint">
          Changing your password signs your other devices out; this one stays
          signed in.
        </p>
        {passwordError && (
          <p className="profile-error" role="alert">
            {passwordError}
          </p>
        )}
        {passwordSaved && (
          <p className="profile-success" role="status">
            {passwordSaved}
          </p>
        )}
        <div className="profile-actions">
          <button
            className="ghost-button"
            disabled={!current || !next || !confirm || passwordBusy}
            onClick={() => void savePassword()}
          >
            {passwordBusy ? "Changing…" : "Change password"}
          </button>
        </div>
      </div>
    </section>
  );
}
