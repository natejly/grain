# A workspace can gain a second member

`WorkspaceInvite` exists in `models.py` and in migration 0013, and is referenced
nowhere else. `Membership(...)` is constructed only by the dev seed and by
`_create_account`, always `role="owner"` of a brand-new workspace. So no
workspace has ever had two members, `role="member"` is never assigned by the
product, `require_owner` is trivially true, and every collaborative feature
already shipped rests on a multi-tenancy the product cannot produce.

## 1. Invitations, end to end (backend)
- [ ] Migration 0039: `workspace_invites.revoked_at` + `revoked_by`. The table
      itself already exists (0013). Verify the chain from an empty DB, both ways.
- [ ] `services/auth/invites.py`, modelled on `services/auth/email.py`:
      `issue_invite` (hash at rest, single live invite per (workspace, email),
      returns the raw token once), `load_invite` (by hash, for preview),
      `accept_invite` (conditional UPDATE claim + membership insert, one
      transaction, rowcount decides the race).
- [ ] `invite_email()` beside the other message builders.
- [ ] Owner routes in `api/admin.py`: `GET/POST /api/admin/invites`,
      `DELETE /api/admin/invites/{invite_id}`.
- [ ] Invitee routes in `api/auth.py`: `GET /api/auth/invites/{token}` (preview,
      unauthenticated — the token is the credential) and
      `POST /api/auth/invites/accept` (session required).
- [ ] The raw link is returned exactly once, in the 201 body of the POST that
      creates it, so an owner can deliver it out of band. That is what makes the
      flow reachable in dev, where `EMAIL_SENDER=console` is the default and
      `_guard_auth` refuses it outside development — no dev-only branch.
- [ ] No account yet: preview works unauthenticated, the page sends them to
      signup and back. Already a member: accept is idempotent, invite is burnt.

## 2. Roles that mean something
- [ ] `require_owner` audit: which routes a member reaches today, written down.
- [ ] A member cannot escalate: every membership/invite write is owner-only.
- [ ] `role` is validated against `{owner, member}` on the way in — a free-text
      role column that accepts "admin" is a role that means nothing.

## 3. Members management
- [ ] `DELETE /api/admin/members/{membership_id}` — remove a member.
- [ ] `PATCH /api/admin/members/{membership_id}` — change a role.
- [ ] The last owner cannot be removed or demoted. A workspace with no owner is
      unreachable, which `api/auth.py` already argues for signup.
- [ ] Removing a member revokes their sessions? No — sessions are per user, not
      per workspace. Removing the membership is enough: `_resolve_workspace`
      fails closed on the next request.

## 4. Web
- [ ] Admin → Members: invite form, pending invites with revoke, role select,
      remove. Existing palette and CSS custom properties only.
- [ ] `/invite?token=...` accept page, modelled on the existing token pages.

## 5. Tests
- [ ] `RouteCase`s for all five new routes.
- [ ] Cross-tenant: an invite for A is unusable against B.
- [ ] Concurrency: two racing accepts produce one membership. Mutation-check.
- [ ] Last-owner guard, role validation, email mismatch, expiry, revoke, reuse.

## 6. Reported, not built
- [ ] What `MemoryItem`, files and `tool_policies` would cost to make per-scope.

## Gates
- [ ] ruff + mypy + pytest (x2) + openapi export
- [ ] tsc + eslint + vitest + build
- [ ] playwright (x2)

## Review
(filled in at the end)
