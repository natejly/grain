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
same trust level as typed text.

Nothing here commits; the route owns the transaction.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import re
import secrets
from dataclasses import dataclass
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Conversation, InboundAddress, Message, Space
from . import conversations

#: Everything a delivery may put into one message. Email bodies are unbounded
#: attacker input; threads are for reading.
MAX_BODY_CHARS = 10000

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
    afterwards (so `&lt;b&gt;` cannot re-become a tag), whitespace collapses.
    """
    without_tags = _TAG_PATTERN.sub(" ", markup or "")
    return re.sub(r"[ \t\r\f\v]+", " ", html_lib.unescape(without_tags)).strip()


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
        # The one creation site that does NOT read the member's preference, and
        # deliberately so. Every other thread starts from something the member
        # typed; this one starts from a body anyone on the internet can send to
        # a published address, and it starts unattended. Seeding it agentic
        # would be handing a stranger's text the writes — exactly the shape the
        # injection screen exists to catch, and a defence that has to fire is
        # worse than a thread that never offered the opening.
        #
        # It is a floor, not a ceiling: the member can pick any mode on the
        # thread once they have read the mail, which is the point at which a
        # person is actually looking at it.
        approval_mode=conversations.SAFE_MODE,
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
