"""Egress rules, and the environment a sandbox is allowed to see.

Two jobs, both of which are "say no by construction rather than by review".

The denied ranges are not configurable. `SANDBOX_NETWORK_POLICY=open` means the
user may reach the internet; it has never meant the sandbox may reach a cloud
metadata endpoint or a private network. Those two are the difference between
"generated code can download a package" and "generated code can mint credentials
for the account the provider runs on", and no operator should be able to turn the
distinction off from a .env file.

The environment allowlist is the other half. Our process environment holds an
OpenAI key, a database URL and the session-signing secret. Generated code runs
with whatever we pass, so the rule is that we pass a constructed dict and never a
copy — `sandbox_env` builds one from scratch, and `test_sandbox_security.py`
asserts that no `SecretStr` field on `Settings` appears in what it returns.
"""
from __future__ import annotations

from typing import Dict, List, Mapping, Tuple

from ...config import Settings
from .types import NetworkPolicy

#: Denied under every policy, `open` included.
#:
#: 169.254.0.0/16 is the one that matters: it holds 169.254.169.254, the cloud
#: metadata endpoint on AWS, GCP and Azure alike, and reading it is the standard
#: first move of an SSRF into credential theft. The RFC1918 ranges and loopback
#: are there because a sandbox has no business reaching its host's neighbours,
#: and ::1/fd00::/fe80:: are the same three facts in IPv6 — omitting them is how
#: this kind of allowlist usually fails.
ALWAYS_DENIED_CIDRS: Tuple[str, ...] = (
    "169.254.0.0/16",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "::1/128",
    "fd00::/8",
    "fe80::/10",
)

#: "Everything", in both address families. Both are needed and the v4 one alone
#: is a trap: `0.0.0.0/0` says nothing about IPv6, so a deny list containing only
#: it leaves every IPv6 destination reachable. That is invisible in a test suite
#: talking to v4 literals and decisive on a dual-stack host, where a hostname
#: resolving to a AAAA record simply goes around the rule.
ALL_TRAFFIC = "0.0.0.0/0"
ALL_TRAFFIC_V6 = "::/0"
EVERYTHING = (ALL_TRAFFIC, ALL_TRAFFIC_V6)


def egress_rules(
    policy: NetworkPolicy, allow_hosts: List[str]
) -> Tuple[bool, List[str], List[str]]:
    """Translate a policy into (internet_enabled, allow_out, deny_out).

    `allowlist` still enables the provider's internet flag — the allowlist is
    enforced by the allow_out rules, and disabling internet outright would make
    the allowlist unreachable rather than restrictive.
    """
    denied = list(ALWAYS_DENIED_CIDRS)
    if policy == "none":
        # Both families, and the always-denied ranges too. Under `none` the
        # internet flag already does the work, but a driver that honoured
        # deny_out and ignored the flag would otherwise get the *strictest*
        # policy wrong — so the list is made to stand on its own.
        return False, [], [*EVERYTHING, *denied]
    if policy == "allowlist":
        # Deny everything, then re-permit the named hosts. Stated in that order
        # because an empty allowlist must mean "nothing", not "everything".
        return True, list(allow_hosts), [*EVERYTHING, *denied]
    return True, [], denied


def sandbox_env(settings: Settings) -> Dict[str, str]:
    """The environment generated code is allowed to see.

    Built, not filtered. A filter over `os.environ` is one forgotten name away
    from leaking a key, and the forgotten name is always the one added last.
    """
    env: Dict[str, str] = {
        # So code can tell it is sandboxed — some libraries behave better when
        # they know they are headless, and it makes support conversations short.
        "JASMINE_SANDBOX": "1",
        "MPLBACKEND": "Agg",
        "PYTHONUNBUFFERED": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    if settings.sandbox_network_policy == "none":
        env["NO_NETWORK"] = "1"
    return env


def session_metadata(*, workspace_id: str, user_id: str) -> Mapping[str, str]:
    """Provider-side labels, so a runaway sandbox is attributable during an
    incident without a database round-trip."""
    return {"workspace_id": workspace_id, "user_id": user_id, "app": "jasmine"}
