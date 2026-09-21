"""Read one web page the model named, as text it can actually use.

`web_search` is the provider's: it searches, it fetches, and this app never
sees a URL. That covers "find me something" and covers nothing about "read
THIS", which is most of what a person pastes into a chat — a changelog, a
spec, the page a colleague linked. `web_fetch` is the other half, and it is
ours end to end, which is why it is gated the way it is:

* The destination goes through `services.tools`' SSRF fetcher, against
  `web_fetch`'s OWN host allowlist (`settings.web_fetch_hosts`), never the
  `/tool` endpoint list. Empty means the tool is not registered at all.
* What comes back is untrusted content, screened by the agent loop under its
  own kind (`web_fetch`) rather than folded in with ordinary tool output, so
  an operator reading `screen.flagged` events can tell a poisoned page from a
  poisoned database row.
* The extraction is stdlib only — `html.parser`, which ships with Python. A
  readability library would be a new dependency on the path that parses
  attacker-authored markup, which is the last path that should grow one.

The extractor is deliberately not a browser. It does not run JavaScript, it
does not resolve layout, and a page that renders its content client-side comes
back nearly empty. That is honest: the alternative is a headless browser, which
is a different order of machinery and a different threat model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Dict, List, Optional

import httpx

from ..config import Settings
from .tools import PAGE_ACCEPT, ToolSecurityError, execute_read_only_get

#: Tags whose *contents* are never prose. Dropped wholesale, contents and all —
#: this is the entire readability heuristic and the reason it is not a
#: heuristic at all: script and style carry code, and nav/header/footer/aside
#: carry the same forty links on every page of a site, which is what buries the
#: one paragraph the model was sent to read.
_DROPPED = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
        "iframe",
        "head",
    }
)

#: Tags that end a line of prose. Everything else is inline.
_BREAKING = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "br",
        "tr",
        "table",
        "ul",
        "ol",
        "blockquote",
        "pre",
        "figure",
        "figcaption",
        "main",
    }
)

_HEADINGS: Dict[str, str] = {
    "h1": "# ",
    "h2": "## ",
    "h3": "### ",
    "h4": "#### ",
    "h5": "##### ",
    "h6": "###### ",
}

#: Collapses the run of blank lines that dropping a nav leaves behind.
_BLANK_RUN = re.compile(r"\n{3,}")


class _Readability(HTMLParser):
    """HTML in, markdown-ish prose out.

    Nesting is tracked with a DEPTH COUNTER per dropped tag rather than a
    boolean, because real pages nest `<div>`s inside `<nav>`s and close them in
    orders no spec would endorse: a boolean flips back on the first `</div>`
    and the rest of the navigation lands in the output. The counter also never
    goes negative, so a stray closing tag — which is common — cannot unbalance
    the rest of the document.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._drop_depth = 0
        self._title = ""
        self._in_title = False

    # -- collection --------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        # Before the drop check, deliberately: `<title>` lives inside `<head>`,
        # which IS dropped, and the title is the one thing in there worth
        # keeping — it is how a citation names the page.
        if tag == "title":
            self._in_title = True
            return
        if tag in _DROPPED:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        if tag in _HEADINGS:
            self._parts.append("\n\n" + _HEADINGS[tag])
        elif tag == "li":
            self._parts.append("\n- ")
        elif tag in _BREAKING:
            self._parts.append("\n\n")

    def handle_startendtag(self, tag: str, attrs: List[tuple]) -> None:
        # A self-closing <br/> must break the line, and a self-closing <nav/>
        # must not leave the drop counter raised for the rest of the document.
        if tag in _DROPPED:
            return
        if self._drop_depth:
            return
        if tag in _BREAKING:
            self._parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in _DROPPED:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if self._drop_depth:
            return
        if tag in _HEADINGS or tag in _BREAKING or tag == "li":
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title += data
            return
        if self._drop_depth or not data.strip():
            return
        self._parts.append(data)

    # -- result ------------------------------------------------------------

    @property
    def title(self) -> str:
        return " ".join(self._title.split())

    def text(self) -> str:
        joined = "".join(self._parts)
        # Per line: collapse internal whitespace, drop the line if nothing is
        # left. Done line-wise rather than globally so the paragraph and list
        # structure the tags above established survives.
        lines = [" ".join(line.split()) for line in joined.split("\n")]
        return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()


def readable_markdown(body: str) -> tuple[str, str]:
    """(title, prose) for one HTML document.

    Non-HTML comes back unchanged apart from whitespace normalisation: a plain
    text or markdown response is already the thing we wanted, and running it
    through an HTML parser would eat every `<` it contains — which in a
    changelog or a code sample is most of the interesting part.
    """
    if "<" not in body:
        return "", _BLANK_RUN.sub("\n\n", body).strip()
    parser = _Readability()
    try:
        parser.feed(body)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup is data, not an error
        # Whatever was parsed before the failure still stands; falling back to
        # the raw body would hand the model a page of tags.
        pass
    # No `html.unescape` here: the parser runs with `convert_charrefs=True`, so
    # entities are already text, and a second pass would turn a page that
    # literally wrote `&amp;lt;` into a page containing `<`.
    return parser.title, parser.text()


@dataclass(frozen=True)
class FetchedPage:
    url: str
    status_code: int
    title: str
    text: str
    #: True when `text` stops before the page did.
    truncated: bool


def fetch_page(
    url: str,
    settings: Settings,
    *,
    transport: Optional[httpx.BaseTransport] = None,
) -> FetchedPage:
    """Fetch and extract one page. Raises `ToolSecurityError` for a refusal.

    The allowlist decision is made HERE and passed down explicitly, rather than
    left to the fetcher's default: the default is the `/tool` endpoint list,
    and silently inheriting it is exactly the mistake that would make every
    owner-registered endpoint reachable by a model-supplied URL.
    """
    if not settings.web_fetch_enabled:
        raise ToolSecurityError("web_fetch is not configured for this deployment")
    status_code, body = execute_read_only_get(
        url,
        settings,
        transport=transport,
        # "*" is the operator's documented opt-out; it relaxes the host list
        # and nothing else. Scheme, DNS and peer checks still run below it.
        require_allowlist=not settings.web_fetch_any_host,
        allow_hosts=settings.web_fetch_hosts,
        accept=PAGE_ACCEPT,
    )
    title, text = readable_markdown(body)
    limit = settings.web_fetch_max_chars
    truncated = len(text) > limit
    if truncated:
        text = text[:limit].rstrip() + "\n\n[…page continues]"
    return FetchedPage(
        url=url,
        status_code=status_code,
        title=title,
        text=text,
        truncated=truncated,
    )
