"use client";

import type { AdminInvite, AdminMember } from "@workspace/api-client";
import { Check, Copy, UserMinus, UserPlus, X } from "lucide-react";
import { type FormEvent, useState } from "react";
import { api } from "../api";
import { describeError, formatRelative } from "./shared";

/**
 * Who is in this workspace, and who has been asked in.
 *
 * Two panels rather than one, because they answer different questions and the
 * second is temporary by nature: "who is here" is the roster, "who has been
 * invited" is a queue that should empty. Splitting them also keeps the roster
 * from growing a `status` column whose only two values would be "member" and
 * "not a member yet".
 *
 * Every write here is owner-only at the API, which is the part that matters:
 * these are the routes that hand out roles, so a member who could reach them
 * could make themselves an owner. Nothing below is a permission check — the
 * whole panel is unreachable for a member, who gets the owner-only sentence in
 * `admin.tsx` instead.
 */

/** The two roles the product has. Anything else is a 422 at the API. */
const ROLES = ["member", "owner"] as const;

export type MembersPanelProps = {
  members: AdminMember[];
  onChange: (members: AdminMember[]) => void;
  setError: (message: string) => void;
};

export function MembersPanel({ members, onChange, setError }: MembersPanelProps) {
  const [busy, setBusy] = useState("");

  async function setRole(member: AdminMember, role: string) {
    if (role === member.role) return;
    setBusy(member.membership_id);
    try {
      const saved = await api.setAdminMemberRole(member.membership_id, role);
      onChange(
        members.map((row) => (row.membership_id === saved.membership_id ? saved : row)),
      );
    } catch (caught) {
      // The last-owner refusal arrives as a 409 whose detail explains itself, so
      // it is shown rather than replaced with a guess about what went wrong.
      setError(describeError(caught, "Could not change that role"));
    } finally {
      setBusy("");
    }
  }

  async function remove(member: AdminMember) {
    const question = member.is_self
      ? "Leave this workspace? You lose access to it immediately."
      : `Remove ${member.name || member.email}? They lose access immediately. ` +
        "Anything they wrote stays.";
    if (!window.confirm(question)) return;
    setBusy(member.membership_id);
    try {
      await api.removeAdminMember(member.membership_id);
      onChange(members.filter((row) => row.membership_id !== member.membership_id));
    } catch (caught) {
      setError(describeError(caught, "Could not remove that member"));
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="admin-panel">
      <div className="panel-title">
        <div>
          <strong>Members and roles</strong>
        </div>
        <span className="panel-count">{members.length}</span>
      </div>
      <div className="admin-table-scroll">
        <table className="admin-table">
          <thead>
            <tr>
              <th scope="col">Member</th>
              <th scope="col">Role</th>
              <th scope="col">Joined</th>
              <th scope="col">
                <span className="visually-hidden">Remove</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {members.map((member) => (
              <tr key={member.membership_id}>
                <td>
                  <strong>{member.name || member.email}</strong>
                  <span>{member.email}</span>
                </td>
                <td>
                  <select
                    className="usage-window"
                    aria-label={`Role for ${member.name || member.email}`}
                    value={member.role}
                    disabled={busy === member.membership_id}
                    onChange={(event) => void setRole(member, event.target.value)}
                  >
                    {ROLES.map((role) => (
                      <option key={role} value={role}>
                        {role}
                      </option>
                    ))}
                  </select>
                  {member.is_self && <span className="admin-tag">you</span>}
                </td>
                <td>{formatRelative(member.joined_at)}</td>
                <td>
                  <button
                    className="ghost-button"
                    disabled={busy === member.membership_id}
                    aria-label={`Remove ${member.name || member.email}`}
                    onClick={() => void remove(member)}
                  >
                    <UserMinus size={12} /> {member.is_self ? "Leave" : "Remove"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="field-hint">
        A workspace always keeps at least one owner. Promote somebody else before
        stepping down or stepping out.
      </p>
    </section>
  );
}

export type InvitesPanelProps = {
  invites: AdminInvite[];
  onChange: (invites: AdminInvite[]) => void;
  setError: (message: string) => void;
};

export function InvitesPanel({ invites, onChange, setError }: InvitesPanelProps) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<string>("member");
  const [busy, setBusy] = useState("");
  const [problem, setProblem] = useState("");
  // The one moment the raw link exists. Held in component state and never
  // persisted: the API stores only its SHA-256, so once this unmounts the only
  // way to get a link for that address again is to invite them again — which
  // mints a new one and revokes this.
  const [link, setLink] = useState("");
  const [copied, setCopied] = useState(false);
  const pending = invites.filter((row) => row.status === "pending");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setProblem("");
    setBusy("form");
    try {
      const created = await api.createInvite(email.trim(), role);
      onChange([created.invite, ...invites]);
      setLink(created.accept_url);
      setCopied(false);
      setEmail("");
    } catch (caught) {
      setProblem(describeError(caught, "Could not send that invitation"));
    } finally {
      setBusy("");
    }
  }

  async function revoke(invite: AdminInvite) {
    setBusy(invite.id);
    try {
      const revoked = await api.revokeInvite(invite.id);
      onChange(invites.map((row) => (row.id === revoked.id ? revoked : row)));
    } catch (caught) {
      setError(describeError(caught, "Could not withdraw that invitation"));
    } finally {
      setBusy("");
    }
  }

  async function copy() {
    // Guarded rather than assumed: the Clipboard API is absent on insecure
    // origins and in some embedded browsers, and the link is on screen and
    // selectable anyway — copying is the convenience, not the mechanism.
    try {
      await navigator.clipboard?.writeText(link);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <section className="admin-panel">
      <div className="panel-title">
        <div>
          <strong>Invitations</strong>
        </div>
        <span className="panel-count">{pending.length}</span>
      </div>

      <form className="budget-form" onSubmit={(event) => void submit(event)}>
        <div className="budget-fields">
          <label htmlFor="invite-email">
            Email
            <input
              id="invite-email"
              type="email"
              autoComplete="off"
              placeholder="person@example.com"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </label>
          <label htmlFor="invite-role">
            Role
            <select
              id="invite-role"
              value={role}
              onChange={(event) => setRole(event.target.value)}
            >
              {ROLES.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </label>
        </div>

        {problem && (
          <p className="budget-problem" role="alert">
            {problem}
          </p>
        )}

        <div className="budget-form-actions">
          <button type="submit" className="primary-button" disabled={busy === "form"}>
            <UserPlus size={13} /> {busy === "form" ? "Inviting…" : "Send invitation"}
          </button>
        </div>
      </form>

      {link && (
        <div className="invite-link" role="status">
          <p className="field-hint">
            The invitation was emailed. This link is shown once and cannot be read
            back — copy it if you would rather send it yourself.
          </p>
          <div className="invite-link-row">
            <code>{link}</code>
            <button
              className="ghost-button"
              aria-label="Copy invitation link"
              onClick={() => void copy()}
            >
              {copied ? <Check size={12} /> : <Copy size={12} />}
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        </div>
      )}

      {invites.length === 0 ? (
        <p className="admin-empty">Nobody has been invited yet.</p>
      ) : (
        <div className="admin-table-scroll">
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">Invited</th>
                <th scope="col">Role</th>
                <th scope="col">Status</th>
                <th scope="col">
                  <span className="visually-hidden">Withdraw</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {invites.map((invite) => (
                <tr key={invite.id}>
                  <td>
                    <strong>{invite.email}</strong>
                    <span>
                      {invite.invited_by_name
                        ? `by ${invite.invited_by_name}, ${formatRelative(invite.created_at)}`
                        : formatRelative(invite.created_at)}
                    </span>
                  </td>
                  <td>
                    <span className="admin-tag">{invite.role}</span>
                  </td>
                  <td>
                    <span className="admin-tag">{invite.status}</span>
                  </td>
                  <td>
                    {invite.status === "pending" && (
                      <button
                        className="ghost-button"
                        disabled={busy === invite.id}
                        aria-label={`Withdraw the invitation to ${invite.email}`}
                        onClick={() => void revoke(invite)}
                      >
                        <X size={12} /> Withdraw
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
