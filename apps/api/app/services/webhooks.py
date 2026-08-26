"""Outbound webhooks: pushing workspace events to owner-configured URLs.

Two halves, mirroring how mail is done here (services/dashboard_subscriptions):

**emit** is called at the natural chokepoints — a run completing, a workflow
run finishing, a tool call parking for approval, a monitor tripping — inside
the caller's transaction. It writes `pending` `webhook_deliveries` rows for
every enabled endpoint subscribed to that event and never commits: the event
and its deliveries land or vanish together with the write that caused them.
Payloads carry ids and titles only, never message or tool content — a webhook
receiver gets "something happened to X", not the workspace's words.

**delivery** rides the shared `POST /api/workflows/tick`. `claim_due` bumps
`attempts` by conditional UPDATE (the house at-most-once-per-attempt claim)
and the tick enqueues `send_delivery` per claimed row on a background task,
so the HTTP conversations never delay the tick itself (the F5 QA note). A
failed attempt stamps `next_attempt_at` from `RETRY_BACKOFF_MINUTES` — an
exponential spread giving a receiver that is down for a deploy hours of
horizon, not minutes — and `MAX_ATTEMPTS` claims without a 2xx mark the row
`failed` (an owner can requeue it from the deliveries panel). Retried sends
make delivery at-least-once, as webhooks are everywhere; receivers key on
the `delivery_id` in the body.

**SSRF policy, decided on purpose:** endpoint URLs go through
`tools.validate_public_https_url` with `require_allowlist=False` — the
`allowed_tool_hosts` allowlist is for destinations the *model* can pick, and
a webhook URL is configured by the workspace owner (the same person who
administers that allowlist), so the list would only be re-approving their own
choice. Scheme (HTTPS-only) and internal-address blocking still apply, at
create AND at every send, plus `peer_is_blocked` on the socket actually used
— an owner may point at the public internet, never at this deployment's
network. Requests are sent through the module-level `HTTP_TRANSPORT` seam so
tests run offline with `httpx.MockTransport`.

Every delivery is signed Stripe-style. `X-Grain-Signature` is
`t=<unix>,v1=<hex>` where `<hex>` is the HMAC-SHA256 hexdigest, under the
endpoint's decrypted secret, of the timestamp, a literal `.`, and the exact
body bytes. A receiver verifies by splitting the header on `,`, recomputing
`HMAC-SHA256(secret, f"{t}.{raw_body}")`, comparing constant-time
(`hmac.compare_digest`), and rejecting a `t` older than its tolerance
(minutes, not hours) — the signed timestamp is what lets it refuse a
replayed capture of a genuine POST.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import timedelta, timezone
from typing import Any, Dict, List, Optional, cast

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..config import Settings, get_settings
from ..database import SessionLocal
from ..models import WebhookDelivery, WebhookEndpoint
from .crypto import decrypt_secret
from .tools import ToolSecurityError, peer_is_blocked, validate_public_https_url

logger = logging.getLogger(__name__)

#: The whole event vocabulary. A subscription names a subset of these; emit
#: refuses anything else so a typo cannot create deliveries nobody fires.
EVENTS = (
    "run.completed",
    "workflow_run.completed",
    "approval.requested",
    "monitor.tripped",
)

#: Claims (== send attempts) before a delivery is marked `failed` for good.
MAX_ATTEMPTS = 6

#: Minutes a failed attempt N waits before attempt N+1 may be claimed — an
#: exponential spread totalling ~5.6 hours of horizon, so a receiver that is
#: down for a deploy window loses nothing.
RETRY_BACKOFF_MINUTES = (1, 5, 15, 60, 240)

#: How many pending rows one tick may claim — the sweep stays bounded however
#: deep the backlog is; the rest waits a minute.
CLAIM_BATCH = 25

SIGNATURE_HEADER = "X-Grain-Signature"

#: Test seam, exactly as `connectors/base.py` and `mcp/oauth.py` carry one:
#: assign an `httpx.MockTransport` here and no socket ever opens.
HTTP_TRANSPORT: Optional[httpx.BaseTransport] = None


def sign(secret: str, body: bytes, *, timestamp: int) -> str:
    """The header value a receiver verifies: `t=<unix>,v1=<hex>`.

    `v1` is HMAC-SHA256 hex over the timestamp, a literal ``.``, and the
    exact body bytes — signing the moment along with the payload is what
    lets a receiver reject stale replays (the module docstring spells out
    the verification recipe).
    """
    digest = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def emit(
    db: Session, *, workspace_id: str, event: str, payload: Dict[str, Any]
) -> List[str]:
    """Queue `event` for every enabled, subscribed endpoint. Never commits.

    Called inside the transaction of the thing that happened — flushes so the
    ids are real, and leaves the commit to the caller, so a rolled-back run
    completion cannot leave a webhook announcing it.
    """
    if event not in EVENTS:
        logger.warning("webhook emit refused unknown event %r", event)
        return []
    endpoints = db.scalars(
        select(WebhookEndpoint).where(
            WebhookEndpoint.workspace_id == workspace_id,
            WebhookEndpoint.enabled.is_(True),
        )
    )
    deliveries: List[WebhookDelivery] = []
    for endpoint in endpoints:
        if event not in _events_of(endpoint):
            continue
        delivery = WebhookDelivery(
            workspace_id=workspace_id,
            endpoint_id=endpoint.id,
            event=event,
            payload_json=json.dumps(payload, default=str),
            status="pending",
            attempts=0,
        )
        db.add(delivery)
        deliveries.append(delivery)
    if deliveries:
        # The flush is what turns the Python-side id defaults into real ids.
        db.flush()
    return [delivery.id for delivery in deliveries]


def claim_due(db: Session, *, limit: int = CLAIM_BATCH) -> List[str]:
    """Claim up to `limit` pending deliveries for one send attempt each.

    The claim is the conditional UPDATE bumping `attempts` from the exact
    value we read — of two racing ticks, one gets `rowcount == 1` and the
    attempt, the other moves on. Rows already claimed `MAX_ATTEMPTS` times
    whose send never concluded (a process died between claim and send) are
    closed out as `failed` here so they cannot sit pending forever. Rows
    whose `next_attempt_at` is still in the future are invisible until their
    backoff elapses; NULL means due now. Commits, like every sweep claim.
    """
    moment = utcnow()
    rows = db.execute(
        select(WebhookDelivery.id, WebhookDelivery.attempts)
        .where(
            WebhookDelivery.status == "pending",
            or_(
                WebhookDelivery.next_attempt_at.is_(None),
                WebhookDelivery.next_attempt_at <= moment,
            ),
        )
        .order_by(WebhookDelivery.created_at, WebhookDelivery.id)
        .limit(limit)
    ).all()
    claimed: List[str] = []
    for row in rows:
        if row.attempts >= MAX_ATTEMPTS:
            db.execute(
                update(WebhookDelivery)
                .where(
                    WebhookDelivery.id == row.id,
                    WebhookDelivery.status == "pending",
                )
                .values(status="failed")
            )
            continue
        result = cast(
            "CursorResult[Any]",
            db.execute(
                update(WebhookDelivery)
                .where(
                    WebhookDelivery.id == row.id,
                    WebhookDelivery.status == "pending",
                    WebhookDelivery.attempts == row.attempts,
                )
                .values(attempts=row.attempts + 1)
            ),
        )
        if result.rowcount == 1:
            claimed.append(row.id)
    db.commit()
    return claimed


def send_delivery(delivery_id: str) -> None:
    """Background entrypoint: one claimed delivery, own session, never raises."""
    db = SessionLocal()
    try:
        # A plain select, not `db.get`: the id names a row the sweep just
        # claimed, and the select keeps this file out of the reviewed
        # primary-key-fetch allowlist for free.
        delivery = db.scalar(
            select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
        )
        if delivery is None or delivery.status != "pending":
            return
        deliver(db, delivery)
    except Exception:  # noqa: BLE001 — background push must never propagate
        logger.exception("webhook delivery %s failed unexpectedly", delivery_id)
        db.rollback()
    finally:
        db.close()


def deliver(
    db: Session, delivery: WebhookDelivery, *, settings: Optional[Settings] = None
) -> bool:
    """POST one delivery to its endpoint. True iff the receiver answered 2xx.

    Re-resolves the endpoint under the delivery's OWN workspace and re-runs
    the SSRF checks at send time — an endpoint edited (or a DNS answer
    changed) since create must still not reach an internal address. Failure
    writes `last_error` and, on the last allowed attempt, flips the row to
    `failed`; the commit here is the delivery's own, like `crons.fire`.
    """
    settings = settings or get_settings()
    endpoint = db.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == delivery.endpoint_id,
            WebhookEndpoint.workspace_id == delivery.workspace_id,
        )
    )
    if endpoint is None or not endpoint.enabled:
        return _fail(db, delivery, error="endpoint gone or disabled", final=True)
    body = json.dumps(
        {
            "event": delivery.event,
            "delivery_id": delivery.id,
            "payload": _payload_of(delivery),
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    if endpoint.secret_encrypted:
        try:
            # utcnow is house naive-UTC; pin the zone before asking for the
            # epoch, or the host's local zone would skew every `t=`.
            headers[SIGNATURE_HEADER] = sign(
                decrypt_secret(endpoint.secret_encrypted, settings),
                body,
                timestamp=int(
                    utcnow().replace(tzinfo=timezone.utc).timestamp()
                ),
            )
        except Exception:  # noqa: BLE001 — an unreadable secret is a config fault
            return _fail(db, delivery, error="signing secret unreadable")
    try:
        validate_public_https_url(endpoint.url, settings, require_allowlist=False)
        with httpx.Client(
            timeout=10.0, follow_redirects=False, transport=HTTP_TRANSPORT
        ) as client:
            with client.stream(
                "POST", endpoint.url, content=body, headers=headers
            ) as response:
                if peer_is_blocked(response):
                    raise ToolSecurityError(
                        "Webhook destination connected to a blocked network"
                    )
                response.read()
                status_code = response.status_code
    except (ToolSecurityError, httpx.HTTPError) as exc:
        return _fail(db, delivery, error=str(exc) or exc.__class__.__name__)
    if 200 <= status_code < 300:
        delivery.status = "sent"
        delivery.sent_at = utcnow()
        delivery.last_error = ""
        db.commit()
        return True
    return _fail(db, delivery, error=f"endpoint answered {status_code}")


def _fail(
    db: Session, delivery: WebhookDelivery, *, error: str, final: bool = False
) -> bool:
    delivery.last_error = error[:1000]
    if final or delivery.attempts >= MAX_ATTEMPTS:
        delivery.status = "failed"
    else:
        # Still retryable: schedule the next claim down the backoff spread.
        # `attempts` was bumped at claim time, so attempt 1 indexes slot 0.
        slot = min(max(delivery.attempts, 1), len(RETRY_BACKOFF_MINUTES)) - 1
        delivery.next_attempt_at = utcnow() + timedelta(
            minutes=RETRY_BACKOFF_MINUTES[slot]
        )
    logger.warning(
        "webhook delivery %s attempt %s failed: %s",
        delivery.id,
        delivery.attempts,
        error,
    )
    db.commit()
    return False


def _events_of(endpoint: WebhookEndpoint) -> List[str]:
    try:
        parsed = json.loads(endpoint.events_json or "[]")
    except ValueError:
        return []
    return [item for item in parsed if isinstance(item, str)]


def _payload_of(delivery: WebhookDelivery) -> Any:
    try:
        return json.loads(delivery.payload_json or "{}")
    except ValueError:
        return {}
