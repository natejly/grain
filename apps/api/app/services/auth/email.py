"""Single-use email tokens and the sender that delivers them.

The sender is a two-implementation seam, not a framework: the console sender
prints the link while you develop, and the SMTP sender is what a deployment
configures. The console sender refuses to print a raw link outside
development/test — a verification URL in a log aggregator is a password reset
anybody with log access can perform.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import smtplib
from dataclasses import dataclass
from datetime import timedelta
from email.message import EmailMessage as MimeMessage
from typing import Any, Optional, Protocol, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ...clock import utcnow
from ...config import Settings
from ...models import EmailToken

logger = logging.getLogger("app.auth.email")

PURPOSE_VERIFY_EMAIL = "verify_email"
PURPOSE_PASSWORD_RESET = "password_reset"
# A passwordless sign-in link. Fits `EmailToken.purpose` (String(24)) beside the
# two above, so it reuses the whole single-use/expiry/hash-at-rest mechanism with
# no schema change.
PURPOSE_LOGIN = "login"

VERIFY_TOKEN_TTL = timedelta(hours=24)
# Deliberately shorter than verification: a reset link is a live credential for
# as long as it is valid.
RESET_TOKEN_TTL = timedelta(minutes=30)
# A magic link *is* a live sign-in credential — the same class as a reset link,
# so it gets the same short window, not verification's 24h. Tightened further
# because clicking it lands you in a session directly, with no password step.
LOGIN_TOKEN_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class OutboundEmail:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    def send(self, message: OutboundEmail) -> None: ...


class ConsoleEmailSender:
    """Development delivery: the link goes to stdout so you can click it."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def send(self, message: OutboundEmail) -> None:
        if not self._settings.is_dev_env:
            # Reachable only if a deployment forgets to configure SMTP. Say what
            # was dropped and to whom, never the token itself.
            logger.warning(
                "email suppressed: EMAIL_SENDER=console outside development "
                "(to=%s subject=%s)",
                message.to,
                message.subject,
            )
            return
        logger.info("email to %s: %s\n%s", message.to, message.subject, message.body)
        print(f"\n--- email to {message.to} ---\n{message.subject}\n{message.body}\n")


class SmtpEmailSender:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def send(self, message: OutboundEmail) -> None:
        settings = self._settings
        mime = MimeMessage()
        mime["From"] = settings.email_from
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.body)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_username and settings.smtp_password:
                smtp.login(
                    settings.smtp_username,
                    settings.smtp_password.get_secret_value(),
                )
            smtp.send_message(mime)


def get_email_sender(settings: Settings) -> EmailSender:
    if settings.email_sender == "smtp":
        return SmtpEmailSender(settings)
    return ConsoleEmailSender(settings)


def send_quietly(sender: EmailSender, message: OutboundEmail) -> None:
    """Deliver, and never let a mail failure change the HTTP answer.

    A raised SMTP error would 500 — but only on the branch that actually sends,
    which would reintroduce exactly the observable difference between a known
    and an unknown address that the auth endpoints exist to hide. It also means
    a flaky mail host cannot make a successful signup, or a successful
    invitation, look like a failed one.
    """
    try:
        sender.send(message)
    except Exception:  # noqa: BLE001 - delivery is best effort by design
        logger.exception("failed to deliver %s to %s", message.subject, message.to)


def normalize_email(raw: str) -> str:
    """Lowercase and trim.

    Storage is case-insensitive by normalization, so Bob@example.com cannot
    become a second account beside bob@example.com — and an invitation to one
    spelling is answerable by the account holding the other. Every path that
    compares an address to a `users.email` must come through here, or two of
    them will disagree about whether the same person is the same person.
    """
    return raw.strip().lower()


def looks_like_email(value: str) -> bool:
    """Intentionally shallow. Deliverability is proven by the mail we send to
    the address, not by a regex, and a stricter pattern only rejects valid
    addresses."""
    if value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".")


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue_email_token(
    db: Session, *, user_id: str, purpose: str, ttl: timedelta
) -> str:
    """Create a single-use token and return its raw value once.

    Any outstanding token for the same purpose is consumed first: two live reset
    links mean two chances for an old, possibly-leaked one to be used.
    """
    now = utcnow()
    db.execute(
        update(EmailToken)
        .where(
            EmailToken.user_id == user_id,
            EmailToken.purpose == purpose,
            EmailToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    raw_token = secrets.token_urlsafe(32)
    db.add(
        EmailToken(
            user_id=user_id,
            purpose=purpose,
            token_hash=hash_token(raw_token),
            expires_at=now + ttl,
            created_at=now,
        )
    )
    return raw_token


def consume_email_token(
    db: Session, *, raw_token: str, purpose: str
) -> Optional[EmailToken]:
    """Redeem a token, or return None. Redemption is a one-way door.

    The claim is a single conditional UPDATE and its rowcount is the verdict, so
    the *database* decides which of two overlapping redemptions of one link wins.
    A SELECT for `consumed_at IS NULL` followed by a Python-side assignment
    cannot decide that: both readers see an unconsumed row and both go on to set
    a password. `WHERE consumed_at IS NULL` is evaluated while the row is locked
    for writing on every engine, so the loser's rowcount is 0 on Postgres as well
    as SQLite.

    The claim is committed on its own, before the caller does anything with the
    token, which is what makes the door one-way in the direction that matters: a
    burnt link with an unchanged password costs the user one more reset mail,
    while a redeemable link whose password write failed is a live credential.
    """
    if not raw_token:
        return None
    now = utcnow()
    token_hash = hash_token(raw_token)
    # The cast is only about typing: an ORM-enabled UPDATE really does return a
    # CursorResult, but Session.execute is annotated as the generic Result.
    claimed = cast(
        "CursorResult[Any]",
        db.execute(
            update(EmailToken)
            .where(
                EmailToken.token_hash == token_hash,
                EmailToken.purpose == purpose,
                EmailToken.consumed_at.is_(None),
                EmailToken.expires_at > now,
            )
            .values(consumed_at=now)
        ),
    ).rowcount
    if not claimed:
        # Unknown, expired, or already redeemed — and the empty write
        # transaction is rolled back rather than left sitting on a lock.
        db.rollback()
        return None
    db.commit()
    return db.scalar(
        select(EmailToken).where(
            EmailToken.token_hash == token_hash,
            EmailToken.purpose == purpose,
        )
    )


def verification_email(settings: Settings, *, to: str, raw_token: str) -> OutboundEmail:
    link = f"{settings.primary_web_origin}/auth/verify?token={raw_token}"
    return OutboundEmail(
        to=to,
        subject="Confirm your email",
        body=f"Confirm your address to finish setting up your workspace:\n{link}\n",
    )


def password_reset_email(
    settings: Settings, *, to: str, raw_token: str
) -> OutboundEmail:
    link = f"{settings.primary_web_origin}/auth/reset?token={raw_token}"
    minutes = int(RESET_TOKEN_TTL.total_seconds() // 60)
    return OutboundEmail(
        to=to,
        subject="Reset your password",
        body=f"Reset your password (link expires in {minutes} minutes):\n{link}\n",
    )


def login_link_email(settings: Settings, *, to: str, raw_token: str) -> OutboundEmail:
    link = f"{settings.primary_web_origin}/auth/login-link?token={raw_token}"
    minutes = int(LOGIN_TOKEN_TTL.total_seconds() // 60)
    return OutboundEmail(
        to=to,
        subject="Your sign-in link",
        body=f"Sign in (link expires in {minutes} minutes):\n{link}\n",
    )


def no_account_email(settings: Settings, *, to: str) -> OutboundEmail:
    """Sent when a password reset is requested for an address with no account.

    The mirror image of :func:`account_exists_email`, and it exists for the same
    reason: the reset endpoint used to send mail only when the address was
    registered, so the *response time* said which it was — one SMTP round trip
    (measured at ~29x the unknown-address path) is a louder oracle than any
    difference in the body ever was. Sending on both branches makes the work
    identical, and the recipient learns something they can act on rather than
    something an attacker can.
    """
    return OutboundEmail(
        to=to,
        subject="Password reset requested",
        body=(
            "Someone asked to reset the password for this address, but it has "
            "no account here. If that was you, you can create one: "
            f"{settings.primary_web_origin}/auth/login\n"
            "If it was not you, no action is needed.\n"
        ),
    )


def account_exists_email(settings: Settings, *, to: str) -> OutboundEmail:
    """Sent when someone signs up with an address that already has an account.

    The HTTP response cannot say the address is taken without becoming an
    account-enumeration oracle, so the truth goes to the mailbox instead — where
    only the actual owner can read it.
    """
    return OutboundEmail(
        to=to,
        subject="You already have an account",
        body=(
            "Someone tried to sign up with this address, which already has an "
            f"account. Sign in instead: {settings.primary_web_origin}/auth/login\n"
            "If that was not you, no action is needed.\n"
        ),
    )
