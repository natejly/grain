"""Password hashing.

argon2id, via the reference binding. Two properties matter more than the choice
of algorithm:

* The library's own ``verify`` is used, never a manual ``==`` on digests. It
  compares in constant time; a byte-by-byte comparison leaks the prefix length
  an attacker got right.
* A missing hash never authenticates — where "missing" means falsy, NULL *or*
  empty string. ``User.password_hash`` is nullable because a Google-only account
  has no password, and "no password set" and "any password matches" are one
  ``if`` apart. :func:`verify_password` takes the Optional directly so no caller
  can forget the check.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from argon2 import PasswordHasher
from argon2 import exceptions as argon2_exceptions
from argon2.low_level import Type

# OWASP's second recommended argon2id profile: 19 MiB, t=2, p=1. It is a
# deliberate floor rather than a maximum — raising memory_cost later is safe
# because verify() reads the parameters out of the stored hash, and
# needs_rehash() then tells us to upgrade the row on the owner's next login.
_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

# Verified against when there is no stored hash, so an unknown account and a
# federated-only account cost the same wall-clock time as a real one. Without
# it, "instant rejection" is a free account-existence oracle.
#
# The plaintext is named rather than inlined so the regression test can assert
# the one password that must never authenticate against an absent hash without
# copying the string and letting the two drift apart.
EQUALIZER_PLAINTEXT = "argon2-timing-equalizer"
_DUMMY_HASH = _HASHER.hash(EQUALIZER_PLAINTEXT)


class PasswordPolicyError(ValueError):
    """The supplied password cannot be accepted."""


@dataclass(frozen=True)
class PasswordCheck:
    """Outcome of a verification attempt."""

    ok: bool
    # True when the stored hash used weaker parameters than the current profile.
    # The caller re-hashes with the plaintext it already has in hand; there is no
    # other moment when we legitimately hold it.
    needs_rehash: bool = False


def validate_password(password: str, *, min_length: int, max_length: int) -> None:
    """Reject passwords we refuse to store, before any hashing work happens."""
    if len(password) < min_length:
        raise PasswordPolicyError(
            f"Password must be at least {min_length} characters"
        )
    if len(password) > max_length:
        raise PasswordPolicyError(
            f"Password must be at most {max_length} characters"
        )


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(stored_hash: Optional[str], password: str) -> PasswordCheck:
    """Check a password against a possibly-absent hash.

    Returns rather than raises, because every failure mode — no hash, malformed
    hash, wrong password — must look identical to the caller and take a similar
    amount of time.
    """
    # `not stored_hash`, not `is None`: an empty string is also "no password",
    # and it is what a legacy import, a NOT NULL backfill or a COALESCE leaves
    # behind. Testing `is None` here made verify_password("", ...) fall through
    # to the dummy hash and then report ok=True for the one caller who guessed
    # the equalizer string — an authenticate-anybody hole in a row nobody
    # thought was a credential. The flag is captured before the fallback so the
    # two decisions cannot drift apart again.
    missing = not stored_hash
    candidate = stored_hash or _DUMMY_HASH
    try:
        _HASHER.verify(candidate, password)
    except (
        argon2_exceptions.VerifyMismatchError,
        argon2_exceptions.VerificationError,
        argon2_exceptions.InvalidHashError,
    ):
        return PasswordCheck(ok=False)
    if missing:
        # The dummy hash matched, which can only happen if someone supplied the
        # equalizer string itself. An absent hash still authenticates nobody.
        return PasswordCheck(ok=False)
    # `candidate`, not `stored_hash`: past the `missing` guard the two are the
    # same string, and only this one is a `str` as far as the type checker is
    # concerned. Reading the parameters back out of the hash argon2 just
    # accepted is also the only sound place to ask whether it is stale.
    return PasswordCheck(ok=True, needs_rehash=_HASHER.check_needs_rehash(candidate))
