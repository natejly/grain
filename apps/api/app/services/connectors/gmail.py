from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ...models import IntegrationAccount, SyncJob
from ..llm_tools import ToolContext, ToolResult, ToolSpec
from .base import ConnectorError, ensure_access_token, http_client
from .landing import create_text_source, upsert_dataset

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
SYNC_MESSAGE_CAP = 50


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _header_value(payload: Dict[str, Any], name: str) -> str:
    for header in payload.get("headers", []):
        if str(header.get("name", "")).lower() == name.lower():
            return str(header.get("value", ""))
    return ""


def _decode_body(data: str) -> str:
    padded = data.replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        return base64.b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _plain_text(payload: Dict[str, Any]) -> str:
    body = payload.get("body") or {}
    if payload.get("mimeType") == "text/plain" and body.get("data"):
        return _decode_body(str(body["data"]))
    for part in payload.get("parts") or []:
        text = _plain_text(part)
        if text:
            return text
    return ""


def fetch_profile(token: str) -> str:
    with http_client() as client:
        response = client.get(f"{GMAIL_API}/profile", headers=_headers(token))
    if response.status_code != 200:
        raise ConnectorError(f"Gmail profile fetch failed ({response.status_code})")
    return str(response.json().get("emailAddress", ""))


def sync(db: Session, account: IntegrationAccount, job: SyncJob) -> Dict[str, Any]:
    token = ensure_access_token(db, account)
    cursor = json.loads(job.cursor_json or "{}")
    params: Dict[str, Any] = {"maxResults": SYNC_MESSAGE_CAP, "q": "newer_than:30d"}
    if cursor.get("page_token"):
        params["pageToken"] = cursor["page_token"]
    with http_client() as client:
        listing = client.get(
            f"{GMAIL_API}/messages", params=params, headers=_headers(token)
        )
        if listing.status_code != 200:
            raise ConnectorError(f"Gmail list failed ({listing.status_code})")
        ids = [item["id"] for item in listing.json().get("messages", [])]
        messages: List[Dict[str, Any]] = []
        for message_id in ids[:SYNC_MESSAGE_CAP]:
            detail = client.get(
                f"{GMAIL_API}/messages/{message_id}",
                params={"format": "full"},
                headers=_headers(token),
            )
            if detail.status_code != 200:
                continue
            messages.append(detail.json())

    if not messages:
        return {"messages": 0}

    sections: List[str] = []
    rows: List[Dict[str, Any]] = []
    for message in messages:
        payload = message.get("payload") or {}
        subject = _header_value(payload, "Subject") or "(no subject)"
        sender = _header_value(payload, "From")
        recipient = _header_value(payload, "To")
        date = _header_value(payload, "Date")
        body = _plain_text(payload)[:8000] or str(message.get("snippet", ""))
        sections.append(
            f"# {subject}\n\nFrom: {sender}\nTo: {recipient}\nDate: {date}\n\n{body}"
        )
        rows.append(
            {
                "message_id": message.get("id", ""),
                "thread_id": message.get("threadId", ""),
                "subject": subject,
                "sender": sender,
                "recipient": recipient,
                "date": date,
                "snippet": str(message.get("snippet", ""))[:300],
            }
        )

    create_text_source(
        db,
        workspace_id=account.workspace_id,
        user_id=account.user_id,
        filename=f"gmail-{job.id[:8]}.md",
        text="\n\n---\n\n".join(sections),
        media_type="text/markdown",
    )
    upsert_dataset(
        db,
        workspace_id=account.workspace_id,
        user_id=account.user_id,
        name="gmail-messages",
        rows=rows,
    )
    return {"messages": len(messages)}


def _search_executor(account_id: str):
    def executor(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
        account = db.get(IntegrationAccount, account_id)
        if account is None or account.workspace_id != context.workspace_id:
            return ToolResult(content="Error: Gmail account is no longer connected.")
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(content="Error: query is required.")
        limit = min(int(args.get("max_results") or 5), 10)
        token = ensure_access_token(db, account)
        with http_client() as client:
            listing = client.get(
                f"{GMAIL_API}/messages",
                params={"maxResults": limit, "q": query},
                headers=_headers(token),
            )
            if listing.status_code != 200:
                return ToolResult(content=f"Gmail search failed ({listing.status_code}).")
            results = []
            for item in listing.json().get("messages", [])[:limit]:
                detail = client.get(
                    f"{GMAIL_API}/messages/{item['id']}",
                    params={"format": "metadata"},
                    headers=_headers(token),
                )
                if detail.status_code != 200:
                    continue
                message = detail.json()
                payload = message.get("payload") or {}
                results.append(
                    {
                        "message_id": message.get("id", ""),
                        "subject": _header_value(payload, "Subject"),
                        "from": _header_value(payload, "From"),
                        "date": _header_value(payload, "Date"),
                        "snippet": str(message.get("snippet", ""))[:200],
                    }
                )
        return ToolResult(content=json.dumps({"messages": results}))

    return executor


def _get_message_executor(account_id: str):
    def executor(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
        account = db.get(IntegrationAccount, account_id)
        if account is None or account.workspace_id != context.workspace_id:
            return ToolResult(content="Error: Gmail account is no longer connected.")
        message_id = str(args.get("message_id") or "").strip()
        if not message_id:
            return ToolResult(content="Error: message_id is required.")
        token = ensure_access_token(db, account)
        with http_client() as client:
            detail = client.get(
                f"{GMAIL_API}/messages/{message_id}",
                params={"format": "full"},
                headers=_headers(token),
            )
        if detail.status_code != 200:
            return ToolResult(content=f"Gmail fetch failed ({detail.status_code}).")
        message = detail.json()
        payload = message.get("payload") or {}
        return ToolResult(
            content=json.dumps(
                {
                    "subject": _header_value(payload, "Subject"),
                    "from": _header_value(payload, "From"),
                    "to": _header_value(payload, "To"),
                    "date": _header_value(payload, "Date"),
                    "body": _plain_text(payload)[:3000],
                }
            )
        )

    return executor


def gmail_tools(account: IntegrationAccount) -> Dict[str, ToolSpec]:
    return {
        "gmail_search": ToolSpec(
            name="gmail_search",
            description=(
                "Search the connected Gmail account (read-only). Supports Gmail "
                "query syntax like from:, subject:, newer_than:."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["query"],
            },
            executor=_search_executor(account.id),
        ),
        "gmail_get_message": ToolSpec(
            name="gmail_get_message",
            description="Fetch one Gmail message body by message_id (read-only).",
            parameters={
                "type": "object",
                "properties": {"message_id": {"type": "string"}},
                "required": ["message_id"],
            },
            executor=_get_message_executor(account.id),
        ),
    }


def account_label(payload: Dict[str, Any], token: Optional[str] = None) -> str:
    if token:
        return fetch_profile(token)
    return str(payload.get("email", ""))
