"""Cross-tenant reach, attacked from the angles the route sweep does not take.

`test_tenant_isolation.py` proves a large and important thing: every route,
pointed wholly at another tenant, refuses. This file attacks the seams that
leaves open.

Three of them, in descending blast radius:

1. **Mixed ownership.** The sweep replaces *every* id in a request with the
   victim's, so a route whose first lookup is workspace-scoped refuses on that
   lookup and the second id is never reached. The interesting request is the one
   that names the caller's own resource first and the victim's second — that is
   the request that gets past the guard and arrives at the unguarded query, if
   there is one. Derived from `ROUTE_CASES` rather than hand-listed, so a new
   two-id route joins this sweep the moment it joins that table.

2. **The workspace selection header.** `X-Workspace-Id` is the one input that
   changes *which* tenant a request is about, and it is honoured after a
   membership check. That makes it worth attacking with the values a membership
   check is normally not asked about: case variants, LIKE wildcards, SQL
   metacharacters, 64 KB, and a workspace the caller really is in but only as a
   member.

3. **Ids that are not ids.** Wildcards, traversal, control characters, and
   lengths chosen to find the place where a lookup stops being an equality test.

Every assertion here is on a status code and on the absence of the victim's ids
and marker strings, exactly as the existing sweep does it.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

import pytest
from isolation import DENY, ROUTE_CASES, SCOPED, RouteCase, Tenant, build_tenant

from app.database import SessionLocal
from app.models import Membership, MemoryItem, Workspace

# --------------------------------------------------------------------------
# Fixtures


@pytest.fixture(scope="module", autouse=True)
def _encryption():
    """`build_tenant` writes a Fernet-encrypted connection secret."""
    from cryptography.fernet import Fernet
    from pydantic import SecretStr

    from app.config import get_settings

    settings = get_settings()
    original = settings.integrations_encryption_key
    settings.integrations_encryption_key = SecretStr(Fernet.generate_key().decode())
    yield
    settings.integrations_encryption_key = original


@pytest.fixture(scope="module")
def attacker(_encryption) -> Tenant:
    return build_tenant("Charlie")


@pytest.fixture(scope="module")
def victim(_encryption) -> Tenant:
    return build_tenant("Delta")


def assert_no_leak(
    response, victim: Tenant, label: str, *, supplied: Iterable[str] = ()
) -> None:
    """No id or marker string of the victim's in the body.

    `supplied` excuses the ids the caller itself put in the request: a refusal
    that names the id it refused ("no card matching <id>") is echoing the
    attacker's own input back at them, which tells them nothing they did not
    already type. Everything else the victim owns is still forbidden.
    """
    text = response.text or ""
    echoed = set(supplied)
    for value in victim.all_ids():
        if value in echoed:
            continue
        assert value not in text, f"{label} leaked {victim.label}'s id {value}"
    for secret in (f"{victim.label} secret", f"{victim.label.lower()} secret"):
        assert secret not in text, f"{label} leaked {victim.label}'s content"


# --------------------------------------------------------------------------
# 1. Mixed ownership: the caller's own id first, the victim's second


def _mixed_url(case: RouteCase, *, own: Tenant, other: Tenant, foreign: str) -> str:
    """`case`'s path with every id from `own` except `foreign`, which is theirs."""
    path = case.template
    for name, kind in case.path_ids.items():
        source = other if name == foreign else own
        path = path.replace("{" + name + "}", source.ids[kind])
    for name, literal in case.path_literals.items():
        path = path.replace("{" + name + "}", literal)
    return path


def _mixed_path_cases() -> List[Tuple[RouteCase, str]]:
    """Every DENY route with more than one id in its path, once per id."""
    pairs: List[Tuple[RouteCase, str]] = []
    for case in ROUTE_CASES:
        if case.verdict != DENY or len(case.path_ids) < 2:
            continue
        for name in case.path_ids:
            pairs.append((case, name))
    return pairs


MIXED_PATH_CASES = _mixed_path_cases()


def test_the_app_still_has_multi_id_routes_to_probe() -> None:
    """A guard on the guard: if `ROUTE_CASES` is refactored so this derivation
    yields nothing, the sweep below would pass by being empty."""
    assert len(MIXED_PATH_CASES) >= 8, MIXED_PATH_CASES


@pytest.mark.parametrize(
    "case,foreign",
    MIXED_PATH_CASES,
    ids=[f"{case.key}::{name}" for case, name in MIXED_PATH_CASES],
)
def test_one_foreign_id_in_an_otherwise_own_path_is_refused(
    case: RouteCase, foreign: str, attacker: Tenant, victim: Tenant
) -> None:
    """The caller owns everything in the URL except one id, which is the
    victim's. The first lookup therefore succeeds, and whatever the route does
    with the second id is what is under test.

    Two legal answers: refuse, or — for a route whose second id is resolved by
    *name* inside the first resource — refuse for that reason. What is not legal
    is 2xx, 5xx, or a body carrying the victim's data.
    """
    url = _mixed_url(case, own=attacker, other=victim, foreign=foreign)
    response = attacker.client.request(
        case.method,
        url,
        params=dict(case.query),
        json=case.body,
        headers={"Idempotency-Key": f"mixed-{abs(hash((case.key, foreign))):016x}"},
    )
    label = f"{case.key} with {foreign} from {victim.label}"
    assert response.status_code < 500, f"{label} raised {response.status_code}"
    assert not (200 <= response.status_code < 300), (
        f"{label} was accepted with {response.status_code}. Body: "
        f"{response.text[:300]}"
    )
    assert_no_leak(
        response, victim, label, supplied=[victim.ids[case.path_ids[foreign]]]
    )


def _mixed_body_cases() -> List[RouteCase]:
    """DENY routes that take a foreign id in the *body*, so the body can be
    aimed at the victim while the path stays the caller's own."""
    return [
        case
        for case in ROUTE_CASES
        if case.verdict == DENY and case.body_ids and case.path_ids
    ]


MIXED_BODY_CASES = _mixed_body_cases()


@pytest.mark.parametrize(
    "case", MIXED_BODY_CASES, ids=[case.key for case in MIXED_BODY_CASES]
)
def test_a_foreign_id_in_the_body_of_an_own_resource_is_refused(
    case: RouteCase, attacker: Tenant, victim: Tenant
) -> None:
    """The path names the caller's own resource — so the route's guard passes —
    and the body then asks it to reach across."""
    url = _mixed_url(case, own=attacker, other=victim, foreign="")
    response = attacker.client.request(
        case.method,
        url,
        params=dict(case.query),
        json=case.json_body(victim),
        headers={"Idempotency-Key": f"mixedbody-{abs(hash(case.key)):016x}"},
    )
    label = f"{case.key} with a foreign body id"
    assert response.status_code < 500, f"{label} raised {response.status_code}"
    assert not (200 <= response.status_code < 300), (
        f"{label} was accepted with {response.status_code}. Body: "
        f"{response.text[:300]}"
    )
    assert_no_leak(
        response,
        victim,
        label,
        supplied=[victim.ids[kind] for kind in case.body_ids.values()],
    )


# --------------------------------------------------------------------------
# 2. The workspace selection header


def _hostile_workspace_ids(own: str, foreign: str) -> Dict[str, str]:
    return {
        "the victim's real id": foreign,
        "the victim's id upper-cased": foreign.upper(),
        "own id upper-cased": own.upper(),
        "own id with a trailing space": own + " ",
        "own id with a leading space": " " + own,
        "own id with a tab": own + "\t",
        "like wildcard": "%",
        "like wildcard padded": "%" + own[8:] + "%",
        "like single-char wildcards": "_" * len(own),
        "own id with wildcards for hyphens": own.replace("-", "_"),
        "sql tautology": "' OR '1'='1",
        "sql union": "x' UNION SELECT id FROM workspaces--",
        "sql comment": own + "'--",
        "traversal": "../" + foreign,
        "null escape": own + "%00",
        "64 kilobytes": "a" * 65536,
        "a json array": '["' + foreign + '"]',
        "a valid uuid nobody owns": "00000000-0000-4000-8000-0000000000ff",
    }


def test_a_hostile_workspace_selection_is_refused_without_leaking(
    attacker: Tenant, victim: Tenant
) -> None:
    """Every shape of a bad selection answers the same 403 as a foreign one.

    The LIKE wildcards are the reason this is not redundant with the existing
    "the header cannot select a foreign workspace" test: an id compared with
    LIKE rather than `=` — a plausible future edit, since several lookups in
    this codebase do use LIKE — would make `%` select the first workspace in
    the table, and only a wildcard probe finds that.

    Deleting the `Membership.workspace_id == requested_workspace_id` clause in
    `auth._resolve_workspace` (app/auth.py:150-153) fails this on the victim's
    real id.
    """
    hostile = _hostile_workspace_ids(attacker.workspace_id, victim.workspace_id)
    for label, value in hostile.items():
        response = attacker.client.get(
            "/api/documents", headers={"X-Workspace-Id": value}
        )
        assert response.status_code == 403, (
            f"X-Workspace-Id {label} answered {response.status_code}, not 403"
        )
        assert_no_leak(response, victim, f"X-Workspace-Id {label}")

    # The control: the caller's own id, exactly, is still accepted.
    accepted = attacker.client.get(
        "/api/documents", headers={"X-Workspace-Id": attacker.workspace_id}
    )
    assert accepted.status_code == 200


SCOPED_GET_ROUTES = [
    case
    for case in ROUTE_CASES
    if case.method == "GET" and case.verdict == SCOPED and not case.path_ids
]


@pytest.mark.parametrize(
    "case", SCOPED_GET_ROUTES, ids=[case.key for case in SCOPED_GET_ROUTES]
)
def test_no_route_honours_a_foreign_workspace_selection(
    case: RouteCase, attacker: Tenant, victim: Tenant
) -> None:
    """The membership check must bite on every route, not just the one the
    existing test probes.

    `X-Workspace-Id` is resolved inside `get_actor`, so this is checked by
    construction — but "by construction" is a claim about a dependency graph,
    and a route that resolved the header itself, or read it before requiring an
    actor, would be invisible to any other test in the suite.
    """
    response = attacker.client.get(
        case.url(victim),
        params=case.params(victim),
        headers={"X-Workspace-Id": victim.workspace_id},
    )
    assert response.status_code in (401, 403), (
        f"{case.key} answered {response.status_code} while selecting "
        f"{victim.label}'s workspace"
    )
    assert_no_leak(response, victim, f"{case.key} under a foreign selection")


def test_selecting_a_workspace_you_only_belong_to_as_a_member_drops_owner_rights(
    attacker: Tenant,
) -> None:
    """Role travels with the selected membership, not with the account.

    A user who owns one workspace and merely belongs to another must not carry
    owner rights across when they switch. Nothing else in the suite selects a
    second workspace *and* calls an owner-only route, so a `require_owner` that
    read the role from the wrong membership would go unnoticed.

    Deleting the `Membership.workspace_id == requested_workspace_id` filter in
    `_resolve_workspace` fails this, because the caller's owner membership in
    their first workspace would answer instead.
    """
    db = SessionLocal()
    try:
        second = Workspace(name="Guest workspace")
        db.add(second)
        db.flush()
        db.add(
            Membership(
                workspace_id=second.id, user_id=attacker.user_id, role="member"
            )
        )
        db.commit()
        second_id = second.id
    finally:
        db.close()

    as_member = {"X-Workspace-Id": second_id}
    assert attacker.client.get("/api/bootstrap", headers=as_member).status_code == 200
    denied = attacker.client.get("/api/admin/members", headers=as_member)
    assert denied.status_code == 403, (
        f"an owner-only route answered {denied.status_code} to a member"
    )

    # The control: the same route in the workspace they do own.
    allowed = attacker.client.get(
        "/api/admin/members", headers={"X-Workspace-Id": attacker.workspace_id}
    )
    assert allowed.status_code == 200


# --------------------------------------------------------------------------
# 3. Ids that are not ids


HOSTILE_IDS = [
    "%",
    "%25",
    "_",
    "_" * 36,
    "*",
    "' OR '1'='1",
    "1;DROP TABLE documents",
    "%00",
    "a%0Ab",
    "a%09b",
    "%20",
    "a" * 5000,
    "..%2F..%2F..%2Fetc%2Fpasswd",
    "%2e%2e%2f",
    "null",
    "None",
    "0",
    "-1",
    "NaN",
    "%7B%22%24ne%22%3Anull%7D",  # {"$ne":null}
]

ID_ROUTES = [
    "/api/documents/{id}",
    "/api/projects/{id}",
    "/api/chunks/{id}",
    "/api/conversations/{id}/messages",
    "/api/runs/{id}/events",
    "/api/db/connections/{id}/schema",
    "/api/integrations/{id}/jobs",
    "/api/documents/{id}/versions",
    "/api/sandbox/{id}/executions",
]


@pytest.mark.parametrize("template", ID_ROUTES)
def test_a_hostile_id_is_a_miss_not_a_match_and_never_an_error(
    template: str, attacker: Tenant, victim: Tenant
) -> None:
    """Wildcards, metacharacters and control characters must all simply miss.

    `%` and `_` are the ones that matter: they are LIKE's wildcards, and the
    difference between "this id does not exist" and "this id matched the first
    row in the table" is the difference between a 404 and a cross-tenant read.
    A 5xx matters for a different reason — it means the value reached something
    that was not expecting a string.
    """
    for value in HOSTILE_IDS:
        response = attacker.client.get(template.replace("{id}", value))
        assert response.status_code < 500, (
            f"{template} with id {value!r} raised {response.status_code}: "
            f"{response.text[:200]}"
        )
        assert not (200 <= response.status_code < 300), (
            f"{template} with id {value!r} matched a row ({response.status_code})"
        )
        assert_no_leak(response, victim, f"{template} with {value!r}")


def test_a_like_wildcard_cannot_widen_a_destructive_memory_match(
    attacker: Tenant,
) -> None:
    """`forget` is the highest-stakes place a caller's raw text reaches a LIKE.

    `resolve_forget_targets` matches on `lower(content) LIKE %needle%`, and what
    it returns is what gets tombstoned. If the needle were interpolated raw then
    `%` would select every active memory in the workspace, and an agent that was
    asked to forget one fact would be handed all of them.

    `_like_pattern` (services/memory.py:556-563) escapes `\\`, `%` and `_` and
    passes `escape="\\\\"`. Removing either the `%` or the `_` replacement fails
    this test.
    """
    from app.services import memory as memory_service

    db = SessionLocal()
    try:
        for content in (
            "the deploy_host is prod-1",
            "an entirely unrelated recollection",
            "one more unrelated recollection",
        ):
            db.add(
                MemoryItem(
                    workspace_id=attacker.workspace_id,
                    kind="fact",
                    content=content,
                    normalized_key=content[:40],
                    status="active",
                )
            )
        db.commit()

        def targets(needle: str) -> List[str]:
            matches, _error = memory_service.resolve_forget_targets(
                db, workspace_id=attacker.workspace_id, content=needle
            )
            return [item.content for item in matches]

        wildcard = targets("%")
        single = targets("deploy_host")
        widened = targets("deploy?host".replace("?", "_"))
        substituted = targets("deployXhost")
    finally:
        db.close()

    assert wildcard == [], f"'%' selected {len(wildcard)} memories to forget"
    assert single == ["the deploy_host is prod-1"]
    assert widened == ["the deploy_host is prod-1"]
    assert substituted == [], "deployXhost matched deploy_host: '_' was a wildcard"


# --------------------------------------------------------------------------
# 4. A cross-tenant existence oracle that is not an id


def test_the_app_slug_namespace_reveals_other_tenants_private_apps(
    attacker: Tenant, victim: Tenant
) -> None:
    """`POST /api/apps` answers 409 for a slug taken by *any* workspace.

    Recorded rather than judged: the slug namespace is global by design — it has
    to be, because `/published/apps/{slug}` is a single public namespace — so a
    409 is the honest answer to "that name is taken". But a *private* app's slug
    is not otherwise discoverable, and this turns a create attempt into a probe
    for one. The check is at api/generated_apps.py:143-150, and it deliberately
    carries no workspace filter.
    """
    taken = victim.ids["app_slug"]
    response = attacker.client.post(
        "/api/apps",
        json={"name": "Mine", "slug": taken, "app_type": "code"},
        headers={"Idempotency-Key": "slug-oracle-0001"},
    )
    free = attacker.client.post(
        "/api/apps",
        json={"name": "Mine", "slug": "definitely-not-taken-9x7", "app_type": "code"},
        headers={"Idempotency-Key": "slug-oracle-0002"},
    )
    assert response.status_code == 409
    assert free.status_code == 201
    # The 409 must at least not hand over anything beyond "taken".
    assert_no_leak(response, victim, "slug collision")


# --------------------------------------------------------------------------
# 5. Nothing above touched the victim


def test_none_of_the_probes_changed_the_victims_data(
    attacker: Tenant, victim: Tenant
) -> None:
    """Runs last. The per-probe assertions catch a refusal that returns foreign
    data; they cannot catch one that mutated on the way to refusing."""
    from test_tenant_isolation import workspace_digest

    before = workspace_digest(victim.workspace_id)
    for case, foreign in MIXED_PATH_CASES:
        attacker.client.request(
            case.method,
            _mixed_url(case, own=attacker, other=victim, foreign=foreign),
            params=dict(case.query),
            json=case.body,
            headers={"Idempotency-Key": f"tamper-{abs(hash((case.key, foreign))):016x}"},
        )
    for case in MIXED_BODY_CASES:
        attacker.client.request(
            case.method,
            _mixed_url(case, own=attacker, other=victim, foreign=""),
            params=dict(case.query),
            json=case.json_body(victim),
            headers={"Idempotency-Key": f"tamperb-{abs(hash(case.key)):016x}"},
        )
    assert workspace_digest(victim.workspace_id) == before, (
        "a mixed-ownership probe changed the victim's data"
    )
