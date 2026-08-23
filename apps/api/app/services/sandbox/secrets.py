"""Workspace secrets the sandbox can read as environment variables.

The "connect stuff" seam. A user registers a credential once; generated code
reads it from the environment (`os.environ["STRIPE_API_KEY"]`) and the value
never touches a prompt, a tool argument, or the transcript. Three rules make
that safe enough to offer:

*Encrypted at rest, decrypted only to inject.* Values are Fernet ciphertext
under the same key the OAuth connectors use. `list_secrets` returns names and
metadata; nothing on any read path returns a value. The one decrypt is
`secret_env`, called by `ensure_session` on the way to `provider.create`.

*Names are validated, so a secret cannot shadow the policy environment.* The
sandbox env is built, not filtered (policy.py), and its keys (`GRAIN_SANDBOX`,
`MPLBACKEND`, …) are load-bearing. A secret named `GRAIN_SANDBOX` that overrode
one of them would be a way to lie to the code about where it is running, so the
create route refuses reserved names and this module refuses them again.

*Reachable outward only when egress allows it.* A secret in the environment can
be exfiltrated by prompt-injected code exactly when that code has a socket —
i.e. never under the default `SANDBOX_NETWORK_POLICY=none`, and otherwise under
the same allowlist every other outbound byte obeys. The coupling is the network
policy's, not a new one; the approval preview names the secrets a run can see so
the approver weighs both at once.
"""
from __future__ import annotations

import re
from typing import Dict, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...clock import utcnow
from ...config import Settings
from ...models import SandboxSecret, new_id
from ..crypto import EncryptionNotConfiguredError, decrypt_secret, encrypt_secret
from .policy import sandbox_env

#: An environment variable name: uppercase, starts with a letter, no surprises.
#: Deliberately stricter than POSIX (which allows lowercase) because a secret is
#: a human-typed constant and a predictable shape is easier to reason about than
#: a permissive one.
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class SecretError(ValueError):
    """A secret could not be stored — a bad name, or encryption is unconfigured.

    A plain ValueError subclass so the route can turn it into a 400 with the
    message intact; nothing here leaks a value or a key.
    """


def _reserved_names() -> set[str]:
    """Env keys the policy layer owns, which a secret must never override.

    Derived from `sandbox_env` under both network policies so the set includes
    `NO_NETWORK`, which only appears under `none` — a secret that could set it
    would let generated code be told the wrong thing about its own sandbox.
    """
    names: set[str] = set()
    for policy in ("none", "open"):
        # `_env_file=None` skips reading the developer's .env so the derived set is
        # exactly the policy keys, independent of the machine this runs on. mypy
        # does not see pydantic-settings' init kwargs, hence the ignore.
        settings = Settings(_env_file=None, sandbox_network_policy=policy)  # type: ignore[call-arg]
        names |= set(sandbox_env(settings))
    return names


def validate_name(name: str) -> str:
    """Return the name if it is a usable env var, else raise `SecretError`."""
    cleaned = name.strip()
    if not NAME_RE.match(cleaned):
        raise SecretError(
            "A secret name must be UPPERCASE letters, digits and underscores, "
            "starting with a letter — e.g. STRIPE_API_KEY."
        )
    if cleaned in _reserved_names() or cleaned.startswith("GRAIN_"):
        raise SecretError(f"“{cleaned}” is reserved by the sandbox and cannot be used.")
    return cleaned


def set_secret(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    name: str,
    value: str,
    settings: Settings,
) -> SandboxSecret:
    """Create or replace a secret. Encrypts before the value is ever stored."""
    clean = validate_name(name)
    if not value:
        raise SecretError("A secret needs a value.")
    try:
        ciphertext = encrypt_secret(value, settings)
    except EncryptionNotConfiguredError as exc:
        # Same failure the connectors surface: no key, no encrypted storage.
        raise SecretError(
            "Secrets need INTEGRATIONS_ENCRYPTION_KEY configured to be stored "
            "encrypted. Set it (see .env.example) and try again."
        ) from exc

    row = db.scalars(
        select(SandboxSecret)
        .where(SandboxSecret.workspace_id == workspace_id)
        .where(SandboxSecret.name == clean)
    ).first()
    if row is None:
        row = SandboxSecret(
            id=new_id(),
            workspace_id=workspace_id,
            name=clean,
            value_enc=ciphertext,
            created_by=user_id,
        )
        db.add(row)
    else:
        row.value_enc = ciphertext
        row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    return row


def list_secrets(db: Session, *, workspace_id: str) -> List[SandboxSecret]:
    """Every secret this workspace holds, by name. Values stay encrypted — the
    caller is a listing, and a listing has no business decrypting anything."""
    return list(
        db.scalars(
            select(SandboxSecret)
            .where(SandboxSecret.workspace_id == workspace_id)
            .order_by(SandboxSecret.name)
        )
    )


def delete_secret(db: Session, *, workspace_id: str, name: str) -> bool:
    """Remove a secret. Returns whether one was there — the route turns a miss
    into a 404 rather than pretending it deleted something."""
    row = db.scalars(
        select(SandboxSecret)
        .where(SandboxSecret.workspace_id == workspace_id)
        .where(SandboxSecret.name == name)
    ).first()
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def secret_names(db: Session, *, workspace_id: str) -> List[str]:
    """Just the names, for the approval preview. No decryption."""
    return list(
        db.scalars(
            select(SandboxSecret.name)
            .where(SandboxSecret.workspace_id == workspace_id)
            .order_by(SandboxSecret.name)
        )
    )


def secret_env(db: Session, *, workspace_id: str, settings: Settings) -> Dict[str, str]:
    """The decrypted secrets, as an env dict to fold into a new session.

    The only decrypt path. Best-effort per row: a secret that will not decrypt
    (the key was rotated after it was stored) is skipped rather than allowed to
    break every session creation — the session still starts, without that one
    credential, which is the degradation the user can actually diagnose.
    """
    env: Dict[str, str] = {}
    for row in list_secrets(db, workspace_id=workspace_id):
        try:
            env[row.name] = decrypt_secret(row.value_enc, settings)
        except Exception:  # noqa: BLE001 — a bad row must not sink the session
            continue
    return env
