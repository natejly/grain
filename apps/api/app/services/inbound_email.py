"""Inbound email → thread: minting addresses and landing deliveries.

The address is `inbox+<token>@<settings.inbound_email_domain>`. The token is
the whole credential and follows the house token rules: `token_urlsafe(32)`
(every character of which is legal in an email local part), shown raw exactly
once at mint time, stored only as a sha256 hexdigest via the per-module
`hash_token` copy, and looked up by hash.

A delivery becomes an ordinary personal thread — `created_by` the minting
member, `shared=False` — plus one user-role message with `run_id=""` (the
`crons._post_message` shape). Deliberately NO agent turn is started: a public
door that made the model act on arbitrary external text would be an
injection funnel; a member replies in-thread when they want the agent
engaged. The body is untrusted but is ordinary user message content — the
same trust level as typed text — and the web renderer keeps it inert:
chat.tsx prints user-role messages as plain text, never markdown, so a
hostile mail cannot auto-load remote images (tracking pixels) or dress a
phishing URL in friendly link text. One thing besides the text is read:
chat.tsx also scans every message for `/apps/<slug>` references, so a mail
can mount a dashboard this workspace has *already published publicly* into
the transcript — sandboxed, snapshot-only, no bindings. That is contained
noise rather than a hole, and the reasoning lives at the call site.

Knowing an address means being able to land mail, so each address carries a
`DAILY_CAP` — mail beyond it is a quiet 200 that writes nothing but the
counter (and one audit row at the trip). The cap is a leaky bucket rather
than a per-calendar-day counter, so it cannot be doubled by waiting for
midnight; `count_delivery` states the exact bound.

Nothing here commits; the route owns the transaction.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..models import Conversation, InboundAddress, Message, Space

#: Everything a delivery may put into one message. Email bodies are unbounded
#: attacker input; threads are for reading.
MAX_BODY_CHARS = 10000

#: Deliveries one address may land in a burst, and the number it earns back
#: per day. Anyone who knows an address can fill threads through it; the cap
#: bounds a flood to a day's reading.
DAILY_CAP = 200

#: The window the cap is quoted over. One landing drains after
#: `WINDOW_SECONDS / DAILY_CAP` seconds (a day / the cap), so an address
#: sitting at the ceiling earns exactly `DAILY_CAP` further landings per
#: rolling day, evenly spaced. Read at call time, not folded into a constant,
#: so a test that lowers `DAILY_CAP` gets a coherent drain rate with it.
WINDOW_SECONDS = 86400

_TOKEN_PATTERN = re.compile(r"inbox\+([A-Za-z0-9_-]{8,128})@", re.IGNORECASE)
_TAG_PATTERN = re.compile(r"<[^>]*>")


@dataclass(frozen=True)
class MintedAddress:
    """A fresh address row and the one copy of its token that will ever exist."""

    address: InboundAddress
    token: str


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def mint(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    label: str,
    target_space_id: str = "",
) -> MintedAddress:
    token = secrets.token_urlsafe(32)
    address = InboundAddress(
        workspace_id=workspace_id,
        token_hash=hash_token(token),
        label=label.strip()[:120],
        target_space_id=target_space_id,
        created_by=user_id,
    )
    db.add(address)
    db.flush()
    return MintedAddress(address=address, token=token)


def token_from_recipient(recipient: str) -> str:
    """The routing token in a recipient string, or "".

    Tolerant of the shapes providers send — `inbox+tok@dom`, `Name
    <inbox+tok@dom>` — because the token, not the framing, is the credential.
    """
    match = _TOKEN_PATTERN.search(recipient or "")
    return match.group(1) if match else ""


def resolve(db: Session, token: str) -> Optional[InboundAddress]:
    """The live address behind a token, or None for unknown AND revoked alike."""
    if not token:
        return None
    address = db.scalar(
        select(InboundAddress).where(InboundAddress.token_hash == hash_token(token))
    )
    if address is None or address.revoked_at is not None:
        return None
    return address


def strip_html(markup: str) -> str:
    """A plain-text rendering of an HTML body, for providers that send only HTML.

    A tag-stripper, not a browser: tags become spaces, entities are unescaped
    afterwards, whitespace collapses. NOTE the ordering's limit: unescaping
    after stripping means `&lt;b&gt;` comes out as the literal text `<b>` —
    that text stays harmless only because chat.tsx renders user-role messages
    (which inbound mail lands as) as plain text, never as markdown or HTML.
    This function reduces noise; the renderer is the safety boundary. If user
    messages ever gain rich rendering, revisit both together.
    """
    without_tags = _TAG_PATTERN.sub(" ", markup or "")
    return re.sub(r"[ \t\r\f\v]+", " ", html_lib.unescape(without_tags)).strip()


@dataclass(frozen=True)
class CapVerdict:
    """What the flood cap says about one landing attempt."""

    #: Whether this mail may land.
    allowed: bool
    #: True only on the single attempt that crosses the cap — the route's
    #: signal to audit the trip once rather than once per refused mail.
    tripped: bool


def count_delivery(
    address: InboundAddress, *, now: Optional[datetime] = None
) -> CapVerdict:
    """Charge one landing attempt to the address's flood cap.

    A leaky bucket, deliberately not a per-calendar-day counter. `rate_level`
    is how many landings are still on the clock and `rate_level_at` is the
    moment that level was last drained; one credit drains every
    `WINDOW_SECONDS / DAILY_CAP` seconds. What that buys, and the bound it
    promises:

    - **no burst larger than `DAILY_CAP`** at any instant, ever. The fixed
      UTC-day window this replaced let a flood spend the whole cap at 23:59
      and the whole cap again at 00:01 — 2x the cap inside two minutes, from
      one address, which is exactly the reading-load the cap exists to bound;
    - **`DAILY_CAP` per rolling day sustained** once the bucket is full: a
      sender who stops flooding earns one landing back every
      `WINDOW_SECONDS / DAILY_CAP` seconds as credits drain.

    The honest ceiling: a 24-hour span that *starts* with an empty bucket can
    still see up to 2x `DAILY_CAP` — a full burst plus a day of drained
    credits — but the second half arrives evenly spaced, never as a second
    burst. Bounding a rolling day to a hard `DAILY_CAP` would need a
    timestamp per delivery (a table), not two columns on the address.

    Two deliberate details about the refusing state:

    - a refused attempt does not add to the level (it clamps at
      `DAILY_CAP + 1`), but it *does* restart the drain clock. A sender who
      keeps hammering therefore never drains — the address stays shut for as
      long as the flood lasts, and reopens a couple of drain intervals after
      it stops. That is the same posture the fixed window had (over the cap
      meant shut until midnight), reached without the midnight;
    - because the clock restarts, `tripped` — the route's audit signal —
      fires once per refusing episode, not once per refused mail. A flood
      faster than the drain rate audits exactly once; the worst a sender
      pacing exactly at the drain rate can produce is one audit per drained
      credit, which is `DAILY_CAP` a day, the same bound as the mail itself.

    Two racing deliveries can both read the same level; the cap is a bound,
    not an exact meter. Flushes nothing; the route's commit persists the
    charge even when the mail is refused.
    """
    moment = now or utcnow()
    drain = WINDOW_SECONDS / DAILY_CAP
    level = address.rate_level
    since = address.rate_level_at
    if since is None or level <= 0 or since > moment:
        # Never used, nothing on the clock, or a row stamped by a clock ahead
        # of ours: the bucket starts here. Anchoring an empty bucket to `now`
        # is also what stops an idle address from banking credit.
        level = max(level, 0)
        since = moment
    else:
        drained = int((moment - since).total_seconds() // drain)
        if drained > 0:
            level = max(0, level - drained)
            # Advance by whole credits only, so the remainder carries instead
            # of being rounded away by a run of frequent deliveries.
            since = since + timedelta(seconds=drained * drain)
        if level <= 0:
            since = moment

    if level > DAILY_CAP:
        # Mid-episode: hold the clamp, restart the clock, audit nothing.
        address.rate_level = level
        address.rate_level_at = moment
        return CapVerdict(allowed=False, tripped=False)
    if level >= DAILY_CAP:
        # Full. This attempt is the one that trips, and `DAILY_CAP + 1` is
        # what remembers that the trip has already been audited.
        address.rate_level = DAILY_CAP + 1
        address.rate_level_at = moment
        return CapVerdict(allowed=False, tripped=True)
    address.rate_level = level + 1
    address.rate_level_at = since
    return CapVerdict(allowed=True, tripped=False)


def deliver(
    db: Session,
    *,
    address: InboundAddress,
    sender: str,
    subject: str,
    body: str,
) -> Tuple[Conversation, Message]:
    """Land one email as a new personal thread plus its message. Flushes only."""
    topic = subject.strip() or (f"from {sender.strip()}" if sender.strip() else "")
    title = f"Email: {topic}" if topic else "Email"
    conversation = Conversation(
        workspace_id=address.workspace_id,
        created_by=address.created_by,
        title=title[:200],
        shared=False,
        space_id=_live_space_id(db, address),
    )
    db.add(conversation)
    db.flush()
    content = (
        f"From: {sender.strip() or 'unknown sender'}\n"
        f"Subject: {subject.strip() or '(no subject)'}\n\n"
        f"{body.strip()[:MAX_BODY_CHARS]}"
    )
    message = Message(
        workspace_id=address.workspace_id,
        conversation_id=conversation.id,
        run_id="",
        created_by=address.created_by,
        role="user",
        content=content,
    )
    db.add(message)
    db.flush()
    return conversation, message


def _live_space_id(db: Session, address: InboundAddress) -> str:
    """The target space if it still exists in this workspace, else "".

    A space's delete cascades over its threads; filing a new thread under a
    dead space id would hide it from every rail. The address row keeps its
    stored target — only this delivery falls back to the plain rail.
    """
    if not address.target_space_id:
        return ""
    space = db.scalar(
        select(Space.id).where(
            Space.id == address.target_space_id,
            Space.workspace_id == address.workspace_id,
        )
    )
    return address.target_space_id if space is not None else ""
