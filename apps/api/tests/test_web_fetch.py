"""`web_fetch`: the SSRF gate, the readability pass, and the screen kind.

Every fixture here is SCRIPTED — a queue of `httpx` responses behind a fake
transport, with DNS monkeypatched to a fixed public address. Nothing in this
file reaches the network, which is the only way to test a fetcher that exists
to refuse certain networks.

The SSRF machinery itself is `services/tools.py`'s and is pinned in
`test_tool_fetch.py`; what is pinned here is that `web_fetch` RIDES it rather
than forking it — its own allowlist, re-validated per redirect hop, with the
peer check still firing.
"""
from __future__ import annotations

import socket
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.services.agent_loop import _screen_kind
from app.services.llm_tools import WEB_FETCH, ToolContext, web_fetch_tools
from app.services.tools import ToolSecurityError
from app.services.web_fetch import fetch_page, readable_markdown

ALLOWED = "docs.example.com"
OTHER = "api.github.com"  # on TOOL_HOST_ALLOWLIST, deliberately NOT on web_fetch's
PUBLIC_IP = "140.82.112.3"
PRIVATE_IP = "10.0.0.7"
METADATA_IP = "169.254.169.254"


def _settings(allowlist: str = ALLOWED, **overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        model_provider="openai",
        openai_api_key=SecretStr("test-key"),
        web_fetch_host_allowlist=allowlist,
        tool_host_allowlist=OTHER,
        **overrides,
    )


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443))
        ],
    )


class _FakeStream:
    def __init__(self, peer: Optional[Tuple[str, int]]) -> None:
        self._peer = peer

    def get_extra_info(self, name: str) -> Optional[Tuple[str, int]]:
        return self._peer if name == "server_addr" else None


class _Hop:
    def __init__(
        self,
        status: int = 200,
        *,
        headers: Optional[dict] = None,
        body: bytes = b"",
        peer: Optional[Tuple[str, int]] = (PUBLIC_IP, 443),
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.peer = peer


class _Scripted(httpx.BaseTransport):
    def __init__(self, hops: List[_Hop]) -> None:
        self._hops = list(hops)
        self.requests: List[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        hop = self._hops.pop(0)
        return httpx.Response(
            hop.status,
            headers=hop.headers,
            content=hop.body,
            extensions={"network_stream": _FakeStream(hop.peer)},
        )


PAGE = b"""
<html><head><title>Release notes</title><style>.a{color:red}</style></head>
<body>
  <nav><a href="/x">Home</a><a href="/y">Docs</a><a href="/z">Blog</a></nav>
  <h1>Release 4.2</h1>
  <p>The scheduler now retries failed jobs.</p>
  <ul><li>Retries are capped at five.</li><li>Backoff is exponential.</li></ul>
  <script>window.track('pageview')</script>
  <footer>Copyright 2026</footer>
</body></html>
"""


# --- the readability pass ----------------------------------------------------


def test_the_extractor_keeps_prose_and_drops_the_furniture():
    title, text = readable_markdown(PAGE.decode())
    assert title == "Release notes"
    assert "# Release 4.2" in text
    assert "The scheduler now retries failed jobs." in text
    assert "- Retries are capped at five." in text
    # Scripts, styles, navigation and footers are what bury the one paragraph
    # the model was sent to read.
    assert "window.track" not in text
    assert "color:red" not in text
    assert "Home" not in text
    assert "Copyright 2026" not in text


def test_nested_divs_inside_a_dropped_tag_stay_dropped():
    """A boolean flag flips back on the first `</div>` and spills the rest of
    the navigation into the output; the depth counter does not."""
    _title, text = readable_markdown(
        "<body><nav><div><div>Menu</div></div></nav><p>Body text.</p></body>"
    )
    assert "Menu" not in text
    assert "Body text." in text


def test_a_stray_closing_tag_cannot_unbalance_the_rest_of_the_document():
    _title, text = readable_markdown("</nav><p>Still here.</p>")
    assert "Still here." in text


def test_plain_text_is_not_run_through_the_html_parser():
    """A changelog full of `<` is mostly `<`, and an HTML parser eats it."""
    _title, text = readable_markdown("if (a < b) return;")
    assert text == "if (a < b) return;"


def test_a_script_only_page_reports_nothing_rather_than_lying():
    _title, text = readable_markdown("<html><body><script>render()</script></body></html>")
    assert text == ""


# --- the fetch gate ----------------------------------------------------------


def test_a_page_on_the_allowlist_comes_back_extracted(public_dns):
    page = fetch_page(
        f"https://{ALLOWED}/notes",
        _settings(),
        transport=_Scripted([_Hop(200, body=PAGE)]),
    )
    assert page.status_code == 200
    assert page.title == "Release notes"
    assert "retries failed jobs" in page.text
    assert page.truncated is False


def test_an_unconfigured_deployment_refuses_before_it_dials(public_dns):
    transport = _Scripted([_Hop(200, body=PAGE)])
    with pytest.raises(ToolSecurityError, match="not configured"):
        fetch_page(f"https://{ALLOWED}/notes", _settings(allowlist=""), transport=transport)
    assert transport.requests == []


def test_web_fetch_does_not_inherit_the_tool_endpoint_allowlist(public_dns):
    """The mistake this guards: `/tool` endpoints are hosts an OWNER registered
    for a different purpose, and silently inheriting that list would make every
    one of them reachable from a model-supplied URL."""
    transport = _Scripted([_Hop(200, body=PAGE)])
    with pytest.raises(ToolSecurityError, match="allowlist"):
        fetch_page(f"https://{OTHER}/zen", _settings(), transport=transport)
    assert transport.requests == []


def test_a_redirect_cannot_walk_off_the_allowlist(public_dns):
    transport = _Scripted(
        [_Hop(302, headers={"location": f"https://{OTHER}/zen"})]
    )
    with pytest.raises(ToolSecurityError, match="allowlist"):
        fetch_page(f"https://{ALLOWED}/notes", _settings(), transport=transport)
    assert len(transport.requests) == 1  # never dialed the second host


def test_a_rebound_private_peer_is_refused(public_dns):
    """The name resolved public for the pre-connect check and the socket landed
    inside anyway. Still caught, because web_fetch rides the same fetcher."""
    transport = _Scripted([_Hop(200, body=PAGE, peer=(PRIVATE_IP, 443))])
    with pytest.raises(ToolSecurityError, match="blocked network"):
        fetch_page(f"https://{ALLOWED}/notes", _settings(), transport=transport)


def test_the_metadata_endpoint_is_refused(public_dns):
    transport = _Scripted([_Hop(200, body=b"creds", peer=(METADATA_IP, 80))])
    with pytest.raises(ToolSecurityError, match="blocked network"):
        fetch_page(f"https://{ALLOWED}/notes", _settings(), transport=transport)


def test_plain_http_is_refused_before_any_allowlist_question(public_dns):
    transport = _Scripted([_Hop(200, body=PAGE)])
    with pytest.raises(ToolSecurityError, match="HTTPS"):
        fetch_page(f"http://{ALLOWED}/notes", _settings(), transport=transport)
    assert transport.requests == []


def test_the_wildcard_relaxes_only_the_host_list(public_dns):
    """An operator's documented opt-out. Scheme and blocked-network checks are
    untouched — which is the difference between a knob and a hole."""
    page = fetch_page(
        "https://anything.example.org/notes",
        _settings(allowlist="*"),
        transport=_Scripted([_Hop(200, body=PAGE)]),
    )
    assert page.title == "Release notes"

    with pytest.raises(ToolSecurityError, match="blocked network"):
        fetch_page(
            "https://anything.example.org/notes",
            _settings(allowlist="*"),
            transport=_Scripted([_Hop(200, body=PAGE, peer=(PRIVATE_IP, 443))]),
        )


def test_the_output_is_bounded_and_says_so(public_dns):
    body = b"<html><body><p>" + (b"word " * 5000) + b"</p></body></html>"
    page = fetch_page(
        f"https://{ALLOWED}/long",
        _settings(web_fetch_max_chars=200),
        transport=_Scripted([_Hop(200, body=body)]),
    )
    assert page.truncated is True
    assert len(page.text) < 300
    assert "page continues" in page.text


def test_the_page_is_asked_for_as_html(public_dns):
    transport = _Scripted([_Hop(200, body=PAGE)])
    fetch_page(f"https://{ALLOWED}/notes", _settings(), transport=transport)
    assert "text/html" in transport.requests[0].headers["accept"]


# --- registration ------------------------------------------------------------


def _context() -> ToolContext:
    return ToolContext(workspace_id="ws", user_id="u", conversation_id="c")


def test_the_family_is_absent_unless_an_operator_configured_it(monkeypatch):
    """Absent, not disabled-and-present: a tool the model can see and cannot
    use costs a round every time it tries one."""
    monkeypatch.setattr(
        "app.services.llm_tools.get_settings", lambda: _settings(allowlist="")
    )
    assert web_fetch_tools(None, _context()) == {}  # type: ignore[arg-type]

    monkeypatch.setattr("app.services.llm_tools.get_settings", _settings)
    assert set(web_fetch_tools(None, _context())) == {WEB_FETCH}  # type: ignore[arg-type]


def test_the_tool_is_read_only_and_never_force_asks(monkeypatch):
    """Read-only, so a delegate child may use it and subject narrowing is the
    only thing keeping it out of a scoped thread — which is the design, stated
    at the family."""
    monkeypatch.setattr("app.services.llm_tools.get_settings", _settings)
    spec = web_fetch_tools(None, _context())[WEB_FETCH]  # type: ignore[arg-type]
    assert spec.read_only is True
    assert spec.force_ask is False


def test_the_family_rides_subject_narrowing_rather_than_the_shared_set():
    """Not in `SHARED_FAMILIES`: fetching arbitrary pages from a panel whose
    visible subject is one document is new injection surface the panel never
    asked for."""
    from app.services import subjects

    assert "web" not in subjects.SHARED_FAMILIES
    for families in subjects.SUBJECT_FAMILIES.values():
        assert "web" not in families


def test_a_fetched_page_is_screened_under_its_own_kind():
    """Kinds are what an operator reads back off `screen.flagged`. "a page off
    the open internet was poisoned" and "a row in this workspace's own database
    was poisoned" are different incidents with different responses."""
    assert _screen_kind(WEB_FETCH) == "web_fetch"
    assert _screen_kind("search_sources") == "tool_output"
    assert _screen_kind("anything_else") == "tool_output"


def test_the_executor_turns_a_refusal_into_a_result_not_an_exception(monkeypatch):
    """A security refusal that killed the run would teach nothing and lose the
    rest of the turn's work."""
    monkeypatch.setattr("app.services.llm_tools.get_settings", _settings)
    spec = web_fetch_tools(None, _context())[WEB_FETCH]  # type: ignore[arg-type]

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise ToolSecurityError("Tool destination is not on the host allowlist")

    monkeypatch.setattr("app.services.llm_tools.fetch_page", refuse)
    result = spec.executor(None, _context(), {"url": f"https://{OTHER}/zen"})  # type: ignore[arg-type]
    assert "Refused" in result.content
    assert "allowlist" in result.content


def test_the_executor_requires_a_url(monkeypatch):
    monkeypatch.setattr("app.services.llm_tools.get_settings", _settings)
    spec = web_fetch_tools(None, _context())[WEB_FETCH]  # type: ignore[arg-type]
    result = spec.executor(None, _context(), {})  # type: ignore[arg-type]
    assert "url is required" in result.content


def test_the_result_labels_the_page_as_untrusted(monkeypatch, public_dns):
    monkeypatch.setattr("app.services.llm_tools.get_settings", _settings)
    monkeypatch.setattr(
        "app.services.llm_tools.fetch_page",
        lambda url, settings: fetch_page(
            url, settings, transport=_Scripted([_Hop(200, body=PAGE)])
        ),
    )
    spec = web_fetch_tools(None, _context())[WEB_FETCH]  # type: ignore[arg-type]
    result = spec.executor(None, _context(), {"url": f"https://{ALLOWED}/notes"})  # type: ignore[arg-type]
    payload: Dict[str, Any] = __import__("json").loads(result.content)
    assert payload["title"] == "Release notes"
    assert "Untrusted data" in payload["note"]
