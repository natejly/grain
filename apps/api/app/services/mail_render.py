"""Inline-CSS HTML fragments for outbound mail bodies.

Pure string builders shared by every feature that mails rendered content
(dashboard subscriptions, digests). Two rules keep them safe and portable:

- Everything interpolated is escaped with :mod:`html` — titles, column names,
  cell values, and link labels/URLs are user- or dataset-controlled text,
  never markup. A cell containing ``<script>`` must arrive as text.
- All styling is inline on the element. Mail clients strip ``<style>`` blocks
  and never fetch external CSS, so an inline ``style=`` attribute is the only
  presentation that survives delivery.

No settings, no database, no I/O: callers assemble the fragments into an
:class:`~app.services.auth.email.OutboundEmail` ``html`` field themselves and
always pair it with a complete plain-text ``body``. That text/HTML parity is a
caller obligation by convention — this module renders the HTML half only, and
nothing here can check that the plain body says the same thing.
"""
from __future__ import annotations

import html
from typing import Sequence
from urllib.parse import urlsplit

_FONT = "font-family:Arial,Helvetica,sans-serif;"

_TITLE_STYLE = _FONT + "font-size:16px;font-weight:600;color:#111827;margin:0 0 8px;"
_TABLE_STYLE = "border-collapse:collapse;" + _FONT + "font-size:14px;color:#1f2937;"
_CELL_STYLE = "padding:6px 12px;border:1px solid #e5e7eb;text-align:left;"
_HEADER_STYLE = _CELL_STYLE + "background:#f9fafb;font-weight:600;"
_BUTTON_STYLE = (
    "display:inline-block;padding:10px 18px;border-radius:6px;"
    "background:#1f2937;color:#ffffff;text-decoration:none;"
    + _FONT
    + "font-size:14px;font-weight:600;"
)


def _cell_text(value: object) -> str:
    """One rendering rule for every cell: None reads as blank, everything else
    as its ``str``, escaped after stringification so a value whose repr grows
    markup (or a plain string containing it) cannot inject."""
    if value is None:
        return ""
    return html.escape(str(value))


def render_table(
    title: str, columns: Sequence[str], rows: Sequence[Sequence[object]]
) -> str:
    """A titled data table as one self-contained HTML fragment.

    An empty ``rows`` still renders the title and header row — an email that
    says "no rows today" beats one whose table silently vanished.
    """
    header_cells = "".join(
        f'<th style="{_HEADER_STYLE}">{html.escape(str(name))}</th>'
        for name in columns
    )
    body_rows = "".join(
        "<tr>"
        + "".join(f'<td style="{_CELL_STYLE}">{_cell_text(value)}</td>' for value in row)
        + "</tr>"
        for row in rows
    )
    return (
        f'<h2 style="{_TITLE_STYLE}">{html.escape(title)}</h2>'
        f'<table style="{_TABLE_STYLE}">'
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{body_rows}</tbody>"
        "</table>"
    )


def render_link_button(label: str, url: str) -> str:
    """A button-styled link. The URL is escaped as an attribute value (quotes
    included), so a URL carrying ``"`` or ``&`` cannot break out of ``href``.

    The URL must be an absolute ``http://`` or ``https://`` address — anything
    else (``javascript:``, ``data:``, a scheme-relative or relative path)
    raises :class:`ValueError`. Escaping keeps a URL from breaking out of the
    attribute; the allowlist keeps the attribute itself from being a payload.
    Every current caller passes a server-built URL (``settings.
    primary_web_origin``), so a raise here is a programming error surfacing,
    never user input winning.
    """
    scheme = urlsplit(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"link button URL must be http(s), got scheme {scheme!r}")
    return (
        '<p style="margin:16px 0;">'
        f'<a href="{html.escape(url, quote=True)}" style="{_BUTTON_STYLE}">'
        f"{html.escape(label)}</a></p>"
    )
