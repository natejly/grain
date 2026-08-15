# Organization tier — a governance layer above Workspace

QM gap: *"Set org-level configuration, a security posture, and which harnesses
and models are available"* — and **"Scopes can only tighten organization-wide
policies."** Today a workspace owner is the highest authority that exists.

## The algebra (the whole feature)

Order the three verdicts by strictness: `allow` (0) < `ask` (1) < `deny` (2).

    final = stricter_of(org_verdict, everything_below)

That single `max` is the one-way rule. It is applied **last**, after the
workspace tier, after the personal tier, after the approval mode, after
`force_ask` — so every mechanism that can loosen has already run and none of
them can reach past it.

Consequences, which are the three bullets in the brief:

- org `deny` → 2, and nothing below is stricter, so the answer is `deny`. No
  allow row, no `auto_writes`, no `DEV_UNRESTRICTED_AGENT` moves it.
- org `ask` → 1. Below may return 1 or 2 and win; a 0 is raised to 1.
- org `allow` → 0, and every value below is ≥ 0, so the org constrains nothing.

## Tasks

- [x] 1. `Organization`, `OrgMembership`, `OrgToolPolicy` models;
      `Workspace.organization_id` NOT NULL.
- [x] 2. Provisioning: `services/orgs.py`, signup mints a personal org with the
      signer as org admin; a `before_flush` listener is the floor that makes an
      orphan workspace unconstructable.
- [x] 3. Migration `0041`, backfilling one org per existing workspace and
      promoting each workspace owner to admin of it.
- [x] 4. `evaluate_policy` clamp — org tier derived from `workspace_id` inside
      the function, so no call site can forget to pass it.
- [x] 5. Org-bounded harnesses and models; bootstrap and `POST /messages`
      both read the bounded list.
- [x] 6. `require_org_admin` + `/api/org` router. No workspace-owner route may
      write an org role.
- [x] 7. Tests, including the mutation proof, and isolation `RouteCase`s.
- [x] 8. Gates.

## Review

**The clamp.** `evaluate_policy` ends with `_stricter(result.policy, org_ceiling)`
over allow(0) < ask(1) < deny(2). It runs *after* the workspace tier, the personal
tier, the approval mode and `force_ask`, so every mechanism that can loosen has
already finished and none can produce a value below the ceiling. The org id is
derived from `workspace_id` **inside** the function rather than taken as a
parameter — there is no argument for a call site to omit, so the ceiling is
unbypassable by construction. `by_mode` is cleared when the clamp moves the
answer, because a bypass that was overruled did not decide anything.

**Two tiers, one sentence.** `_in_scope_or_carried_deny` is shared by the org and
workspace tiers: a row in this scope decides, else a `chat` deny carries, else the
tier is silent. Not a coincidence to maintain by hand.

**Mutation proof.** Replacing the clamp with `clamped = result.policy` and running
`test_org_scope.py` failed 8 tests; the headline one reported
`assert 'allow' == 'deny'` on the first assertion of
`test_an_org_deny_cannot_be_loosened_by_anything_below_it` — a workspace `allow`
walking straight through an org `deny`. Restored, all 20 pass.

**Migration.** Verified three ways: fresh chain from empty (0001 `create_all`
already carries the schema, every 0041 block is a guarded no-op); a
downgrade-then-seed-then-upgrade round trip on a genuine pre-0041 database with
two workspaces and three memberships — 0 orphans, 2 distinct orgs, only the two
*owners* promoted to admin, NOT NULL and the FK both present after the batch
rebuild; and `test_personal_scope_migration.py`'s narrower legacy schema, which is
why the backfill reads only `workspaces.id`/`name` and tolerates a missing
`memberships` table.

**Deferred, and why.**

- *Org-level harness/model config has no UI.* The panel edits the model bound and
  the tool ceilings; the harness bound is read-only there (it shows the effective
  count) and is settable only over the API. The harness is a process-wide setting
  with two registered values, so a picker would be a control with nothing much to
  pick — worth adding when a third harness ships.
- *No org member management UI.* `GET/PUT /api/org/members` exist and are tested;
  the panel does not render a roster. Granting org standing is a rare,
  high-consequence action and the workspace roster right beside it made the two
  easy to confuse in a first draft.
- *Audit events for org actions are filed against the acting workspace.*
  `audit_events` is workspace-scoped, and giving it a nullable `organization_id`
  would have been a second migration on the busiest table in the schema for a
  reporting nicety. The event names the org as its `resource_id`, so nothing is
  lost, but "show me everything that happened to this org" is a query across
  workspaces rather than a filter.
- *`OrgToolPolicy` has no `owner_id`.* Deliberate, not deferred — there is nobody
  below the org whose preference an org row would be expressing.
