"""Mail HTML infrastructure: the render helpers are pure and escape everything
they interpolate, the SMTP sender assembles multipart/alternative when a
message carries HTML, the console sender stays text-only, and the capture
seam (monkeypatching get_email_sender) still observes every field.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.services import mail_render
from app.services.auth import email as email_service

# --- render helpers -----------------------------------------------------------


def test_render_table_contains_title_columns_and_cells():
    fragment = mail_render.render_table(
        "Daily revenue", ["region", "total"], [["emea", 12], ["apac", 34]]
    )
    assert "Daily revenue" in fragment
    assert "<table" in fragment and "</table>" in fragment
    assert "<th" in fragment and "region" in fragment and "total" in fragment
    assert "emea" in fragment and "12" in fragment
    assert "apac" in fragment and "34" in fragment
    # Inline CSS only — mail clients strip <style> blocks.
    assert "<style" not in fragment
    assert 'style="' in fragment


def test_render_table_escapes_title_columns_and_cell_values():
    fragment = mail_render.render_table(
        '<script>alert("t")</script>',
        ['<img src=x onerror="c">'],
        [['</td><script>alert("v")</script>']],
    )
    assert "<script" not in fragment
    assert "<img" not in fragment
    assert "&lt;script&gt;" in fragment
    assert "&lt;img" in fragment
    # The cell's closing-tag smuggle arrives as text, not structure.
    assert fragment.count("</td>") == 1


def test_render_table_stringifies_cells_and_renders_none_as_blank():
    fragment = mail_render.render_table("t", ["a", "b", "c"], [[None, 1.5, True]])
    assert "None" not in fragment
    assert "1.5" in fragment
    assert "True" in fragment


def test_render_table_with_no_rows_still_renders_the_header():
    fragment = mail_render.render_table("Empty", ["only column"], [])
    assert "Empty" in fragment
    assert "only column" in fragment
    assert "<tbody></tbody>" in fragment


def test_render_link_button_escapes_label_and_url():
    fragment = mail_render.render_link_button(
        "<b>Open</b> dashboard", 'https://example.com/d?a=1&b="><script>'
    )
    assert "<b>" not in fragment
    assert "<script" not in fragment
    assert "&lt;b&gt;Open&lt;/b&gt; dashboard" in fragment
    # The URL is escaped as an attribute value: quotes cannot close href.
    assert 'href="https://example.com/d?a=1&amp;b=&quot;&gt;&lt;script&gt;"' in fragment


def test_render_link_button_refuses_every_non_http_scheme():
    """Escaping keeps a URL inside the href attribute; the allowlist keeps the
    attribute from being a payload. Callers pass server-built URLs, so a raise
    is a programming error surfacing, never user input winning."""
    for url in (
        "javascript:alert(1)",
        "JAVASCRIPT:alert(1)",
        "data:text/html,<script>x</script>",
        "vbscript:msgbox",
        "//example.com/scheme-relative",
        "/relative/path",
        "",
    ):
        with pytest.raises(ValueError):
            mail_render.render_link_button("Open", url)
    # The two allowed schemes still render.
    assert 'href="http://example.com"' in mail_render.render_link_button(
        "Open", "http://example.com"
    )
    assert 'href="https://example.com"' in mail_render.render_link_button(
        "Open", "https://example.com"
    )


def test_web_origin_cannot_boot_in_a_form_the_link_button_refuses():
    """The allowlist above and `WEB_ORIGIN` are two halves of one contract.

    `primary_web_origin` is what every mailer hands `render_link_button`, so a
    scheme-less `WEB_ORIGIN=app.example.com` would boot perfectly and then
    raise on *every* digest and *every* subscription mail, forever, invisibly.
    The guard belongs at the boundary where an operator sees the message: boot.
    """
    for origin in (
        "app.example.com",
        "//app.example.com",
        "ftp://app.example.com",
        "javascript:alert(1)",
        # A good first entry does not excuse a dead CORS entry behind it.
        "https://app.example.com,app.example.net",
    ):
        with pytest.raises(ValidationError, match="WEB_ORIGIN"):
            Settings(_env_file=None, app_env="test", web_origin=origin)

    # The shapes a real deployment uses still boot, trailing slash included.
    for origin in (
        "http://localhost:3000",
        "https://app.example.com/",
        "https://app.example.com,http://localhost:3000",
    ):
        settings = Settings(_env_file=None, app_env="test", web_origin=origin)
        # And what boots is exactly what the link button accepts.
        assert mail_render.render_link_button("Open", settings.primary_web_origin)


# --- SMTP multipart assembly --------------------------------------------------


class FakeSmtp:
    """Stands in for smtplib.SMTP; records the MIME message instead of talking
    to a mail host."""

    last: FakeSmtp | None = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.sent: list = []
        self.started_tls = False
        FakeSmtp.last = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, mime):
        self.sent.append(mime)


def test_smtp_sender_builds_multipart_alternative_when_html_is_present(monkeypatch):
    monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSmtp)
    sender = email_service.SmtpEmailSender(get_settings())
    sender.send(
        email_service.OutboundEmail(
            to="member@example.com",
            subject="Daily digest",
            body="plain text digest",
            html=mail_render.render_table("Digest", ["n"], [[1]]),
        )
    )
    assert FakeSmtp.last is not None
    (mime,) = FakeSmtp.last.sent
    assert mime.get_content_type() == "multipart/alternative"
    text_part = mime.get_body(preferencelist=("plain",))
    html_part = mime.get_body(preferencelist=("html",))
    assert "plain text digest" in text_part.get_content()
    assert "<table" in html_part.get_content()
    assert mime["To"] == "member@example.com"
    assert mime["Subject"] == "Daily digest"


def test_smtp_sender_stays_plain_text_when_html_is_absent(monkeypatch):
    monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSmtp)
    sender = email_service.SmtpEmailSender(get_settings())
    sender.send(
        email_service.OutboundEmail(
            to="member@example.com", subject="Reset", body="just text"
        )
    )
    (mime,) = FakeSmtp.last.sent
    assert mime.get_content_type() == "text/plain"
    assert "just text" in mime.get_content()


# --- console sender and capture seam ------------------------------------------


def test_console_sender_prints_the_text_part_only(capsys):
    sender = email_service.ConsoleEmailSender(get_settings())
    sender.send(
        email_service.OutboundEmail(
            to="dev@example.com",
            subject="Alert",
            body="the text body",
            html="<table><tr><td>markup-noise</td></tr></table>",
        )
    )
    out = capsys.readouterr().out
    assert "the text body" in out
    assert "markup-noise" not in out
    assert "<table" not in out


def test_capture_seam_still_observes_messages_and_html_defaults_to_empty(monkeypatch):
    captured: list[email_service.OutboundEmail] = []

    class Capturing:
        def send(self, message: email_service.OutboundEmail) -> None:
            captured.append(message)

    monkeypatch.setattr(
        email_service, "get_email_sender", lambda settings: Capturing()
    )
    sender = email_service.get_email_sender(get_settings())
    email_service.send_quietly(
        sender,
        email_service.OutboundEmail(to="a@example.com", subject="s", body="b"),
    )
    (message,) = captured
    assert message.html == ""


def test_send_quietly_swallows_a_failing_sender():
    class Exploding:
        def send(self, message: email_service.OutboundEmail) -> None:
            raise RuntimeError("mail host down")

    email_service.send_quietly(
        Exploding(),
        email_service.OutboundEmail(
            to="a@example.com", subject="s", body="b", html="<p>h</p>"
        ),
    )
