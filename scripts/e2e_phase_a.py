#!/usr/bin/env python3
"""Production A2A smoke for the Codex + DSH release profile."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common.preflight import parse_env  # noqa: E402

ENV = parse_env(ROOT / ".env")
BASE = os.environ.get(
    "LAS_AGENTHUB_SMOKE_BASE", "http://127.0.0.1:8300/agenthub")
TOKEN = ENV.get("LAS_HERMES_GATEWAY_API_KEY", "")
FAILURE_STATES = {"failed", "canceled", "rejected"}


def _error_body(exc: urllib.error.HTTPError) -> dict:
    raw = exc.read()
    try:
        value = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {"body": raw.decode(errors="replace")}
    return value if isinstance(value, dict) else {"body": value}


def call(method: str, params: dict) -> tuple[int, dict]:
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }
    request = urllib.request.Request(
        BASE.rstrip("/") + "/a2a", headers=headers,
        data=json.dumps({
            "jsonrpc": "2.0", "id": "production-smoke",
            "method": method, "params": params,
        }).encode(),
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, _error_body(exc)


def get(path: str, *, authenticated: bool = True) -> tuple[int, dict]:
    request = urllib.request.Request(BASE.rstrip("/") + path)
    if authenticated:
        request.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, _error_body(exc)


def control(action: str, *, context_id: str | None = None, **params) -> dict:
    command = {"agenthub": "v1", "action": action, **params}
    message = {
        "role": "user",
        "parts": [{
            "text": json.dumps(command, ensure_ascii=False),
            "mediaType": "application/json",
        }],
    }
    if context_id:
        message["contextId"] = context_id
    status, response = call("SendMessage", {"message": message})
    if status != 200 or "error" in response:
        raise RuntimeError(f"{action} failed: HTTP {status}: {response}")
    return response["result"]


def wait_for_acceptance(
        task_id: str, context_id: str, timeout: float = 600) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = control("tasks/get", context_id=context_id, task_id=task_id)[
            "task"]
        state = task["status"]["state"]
        kind = task.get("metadata", {}).get("input_required_kind")
        if state == "input-required" and kind == "acceptance":
            return task
        if state in FAILURE_STATES:
            raise RuntimeError(f"task failed before acceptance: {task}")
        if state == "input-required":
            raise RuntimeError(
                f"unexpected {kind or 'unknown'} input request: {task_id}")
        time.sleep(2)
    raise TimeoutError(f"task did not reach acceptance within {timeout}s: {task_id}")


def main() -> int:
    if len(TOKEN) < 16:
        raise RuntimeError("LAS_HERMES_GATEWAY_API_KEY is missing or too short")
    assert get("/.well-known/agent-card.json", authenticated=False)[0] == 401
    status, card = get("/.well-known/agent-card.json")
    assert status == 200 and card["name"] == "agenthub-orchestrator"
    print("PASS: gateway authentication and agent card")

    discovered = control("agents/list")
    agents = {item["id"]: item for item in json.loads(
        discovered["message"]["parts"][0]["text"])["agents"]}
    assert all(agents[name]["enabled"] and agents[name]["online"]
               for name in ("codex", "dsh"))
    print("PASS: Codex and DSH are enabled and online")

    context_id = f"production-smoke-{uuid.uuid4().hex}"
    for worker in ("codex", "dsh"):
        created = control(
            "tasks/create",
            context_id=context_id,
            agent=worker,
            objective=(
                "只读验收：运行 pwd -P 检查当前工作目录，不得修改任何文件；"
                "最终回复必须包含 pwd 的绝对路径。"),
            project="agentHub",
            workspace=str(ROOT),
        )["task"]
        pending = wait_for_acceptance(created["id"], context_id)
        assert pending["metadata"]["internal_status"] == "awaiting_acceptance"
        assert pending["metadata"]["execution_workspace"] == str(ROOT)
        message = pending["status"]["message"]["parts"][0]["text"]
        assert str(ROOT) in message, pending
        assert pending["artifacts"], pending

        accepted = control(
            "tasks/accept", context_id=context_id, task_id=created["id"],
            notes="production read-only workspace smoke passed",
        )["task"]
        assert accepted["status"]["state"] == "completed", accepted
        assert accepted["metadata"]["internal_status"] == "accepted", accepted
        print(f"PASS: {worker} workspace task accepted: {created['id']}")

    gated = control(
        "tasks/create",
        context_id=context_id,
        agent="codex",
        objective="在工作区写入 production-smoke-must-not-exist.txt",
        project="agentHub",
        workspace=str(ROOT),
    )["task"]
    assert gated["status"]["state"] == "input-required", gated
    assert gated["metadata"]["input_required_kind"] == "delegation", gated
    rejected = control(
        "tasks/reject", context_id=context_id, task_id=gated["id"])["task"]
    assert rejected["status"]["state"] == "canceled", rejected
    assert not (ROOT / "production-smoke-must-not-exist.txt").exists()
    print("PASS: write task stayed gated and was rejected without delegation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
