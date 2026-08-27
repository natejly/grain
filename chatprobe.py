"""Scratch tool: run a scenario of chat turns against the LIVE-model API and
report exactly what came back.

Exists so a scenario is a small JSON file rather than fifty lines of auth, CSRF,
multipart upload and SSE parsing repeated per probe. Everything it reports is
observed from the wire: the assistant's own words, the tools it chose, the
citations it claimed, and the terminal status of the run.

Usage:
    python3 chatprobe.py scenario.json            # -> JSON on stdout
    python3 chatprobe.py scenario.json --pretty   # -> readable transcript

Scenario shape:
    {
      "name": "grounded-qa",
      "sources": [{"filename": "notes.md", "content": "..."}],
      "turns": ["first prompt", "second prompt"],
      "fresh_conversation": true
    }
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.cookiejar import CookieJar
from typing import Any, Optional

BASE = "http://127.0.0.1:8011"
EMAIL = "demo@example.com"
PASSWORD = "e2e-demo-password"
# A turn on a reasoning model with tool calls is not fast. Generous, because a
# timeout reported as a quality failure would be a lie about the product.
RUN_TIMEOUT = 420.0


class Client:
    def __init__(self) -> None:
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.csrf = ""
        self.workspace = ""
        # The session cookie is issued `Secure`, and http.cookiejar refuses to
        # replay a Secure cookie over plain http — so every request here would
        # arrive signed out. Browsers exempt 127.0.0.1 as a trustworthy origin,
        # which is why the app itself is fine; this probe is not a browser, so
        # it carries the cookie by hand.
        self.cookie = ""

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        content_type: str = "application/json",
        stream: bool = False,
    ):
        request = urllib.request.Request(BASE + path, data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", content_type)
        if method not in ("GET", "HEAD") and self.csrf:
            request.add_header("X-CSRF-Token", self.csrf)
            # Idempotency is required on the unsafe chat routes; a fresh key per
            # call means these probes never replay each other's runs.
            request.add_header("Idempotency-Key", str(uuid.uuid4()))
        if self.workspace:
            request.add_header("X-Workspace-Id", self.workspace)
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        response = self.opener.open(request, timeout=RUN_TIMEOUT)
        return response if stream else json.loads(response.read().decode() or "{}")

    def login(self) -> None:
        request = urllib.request.Request(
            BASE + "/api/auth/login",
            data=json.dumps({"email": EMAIL, "password": PASSWORD}).encode(),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        response = self.opener.open(request, timeout=RUN_TIMEOUT)
        raw = response.headers.get_all("Set-Cookie") or []
        self.cookie = "; ".join(value.split(";", 1)[0] for value in raw)
        session = json.loads(response.read().decode())
        self.csrf = session["csrf_token"]
        self.workspace = session["workspace_id"]

    def upload_source(self, filename: str, content: str) -> dict:
        boundary = "----chatprobe" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
            f"{content}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        return self._request(
            "POST",
            "/api/sources",
            body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    def new_conversation(self) -> str:
        return self._request("POST", "/api/conversations", b"{}")["id"]

    def send(self, conversation_id: str, content: str) -> dict:
        return self._request(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            json.dumps({"content": content}).encode(),
        )

    def follow(self, run_id: str) -> dict:
        """Drain one run's SSE stream into the facts worth judging."""
        collected: dict[str, Any] = {
            "answer": "",
            "tools": [],
            "citations": None,
            "status": "",
            "error": "",
            "events": [],
            "thinking_chars": 0,
        }
        started = time.time()
        response = self._request("GET", f"/api/runs/{run_id}/events?after=0", stream=True)
        event_type = ""
        for raw in response:
            if time.time() - started > RUN_TIMEOUT:
                collected["error"] = "probe timed out waiting for the run"
                break
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("event: "):
                event_type = line[7:].strip()
                continue
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            collected["events"].append(event_type)
            if event_type == "message.delta":
                collected["answer"] += str(data.get("delta", ""))
            elif event_type == "message.completed":
                # Authoritative over the accumulated deltas.
                collected["answer"] = str(data.get("content", collected["answer"]))
            elif event_type == "thinking.delta":
                collected["thinking_chars"] += len(str(data.get("delta", "")))
            elif event_type == "run.citations":
                collected["citations"] = data
            elif event_type in ("tool.proposed", "tool.running", "tool.completed", "tool.failed"):
                collected["tools"].append(
                    {
                        "stage": event_type.split(".", 1)[1],
                        "name": data.get("tool_name", ""),
                        "arguments": str(data.get("arguments", ""))[:400],
                        "preview": str(data.get("preview", ""))[:400],
                        "error": str(data.get("error", ""))[:300],
                    }
                )
            elif event_type in ("run.completed", "run.failed", "run.cancelled"):
                collected["status"] = event_type.split(".", 1)[1]
                if data.get("error"):
                    collected["error"] = str(data["error"])[:400]
            elif event_type == "run.waiting_for_approval":
                # The park IS the outcome. The run stays open until a human
                # decides, and nobody is watching, so waiting the full timeout
                # buys nothing and costs seven minutes per parked turn.
                collected["status"] = "waiting_for_approval"
                break
        collected["seconds"] = round(time.time() - started, 1)
        return collected


def run_scenario(spec: dict) -> dict:
    client = Client()
    client.login()
    result: dict[str, Any] = {"name": spec.get("name", "unnamed"), "turns": []}

    for source in spec.get("sources", []):
        try:
            uploaded = client.upload_source(source["filename"], source["content"])
            result.setdefault("sources", []).append(
                {"filename": source["filename"], "id": uploaded.get("id", ""), "ok": True}
            )
        except urllib.error.HTTPError as failure:
            result.setdefault("sources", []).append(
                {
                    "filename": source["filename"],
                    "ok": False,
                    "error": f"{failure.code} {failure.read().decode()[:200]}",
                }
            )
    # Indexing is not instantaneous; a question asked before it lands would
    # measure the race rather than the answer.
    if spec.get("sources"):
        time.sleep(spec.get("index_wait", 6))

    conversation = client.new_conversation()
    result["conversation_id"] = conversation

    for prompt in spec.get("turns", []):
        turn: dict[str, Any] = {"prompt": prompt}
        try:
            sent = client.send(conversation, prompt)
            run = sent.get("run") or {}
            if not run:
                turn["error"] = "no run was started"
            else:
                turn.update(client.follow(run["id"]))
        except urllib.error.HTTPError as failure:
            turn["error"] = f"HTTP {failure.code}: {failure.read().decode()[:300]}"
        except Exception as failure:  # noqa: BLE001 - a probe reports, never raises
            turn["error"] = f"{type(failure).__name__}: {failure}"
        result["turns"].append(turn)
    return result


def main() -> None:
    spec = json.loads(open(sys.argv[1]).read())
    result = run_scenario(spec)
    if "--pretty" in sys.argv:
        print(f"### scenario: {result['name']}  (conversation {result['conversation_id']})")
        for source in result.get("sources", []):
            print(f"  source {source['filename']}: ok={source['ok']} {source.get('error','')}")
        for index, turn in enumerate(result["turns"], 1):
            print(f"\n--- turn {index} ---")
            print(f"PROMPT: {turn['prompt']}")
            print(f"STATUS: {turn.get('status','?')}  {turn.get('seconds','?')}s")
            if turn.get("error"):
                print(f"ERROR: {turn['error']}")
            for tool in turn.get("tools", []):
                if tool["stage"] in ("completed", "failed", "proposed"):
                    print(f"TOOL[{tool['stage']}] {tool['name']}: {tool['preview'][:160]}")
            if turn.get("citations"):
                print(f"CITATIONS: {json.dumps(turn['citations'])[:300]}")
            print(f"ANSWER:\n{turn.get('answer','')}")
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
