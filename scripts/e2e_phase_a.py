#!/usr/bin/env python3
"""Production A2A smoke for the Codex + DSH release profile."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common.preflight import parse_env  # noqa: E402

BASE = "http://127.0.0.1:8310"
TERMINAL = {"completed", "failed", "canceled", "rejected"}


def _peer_tokens() -> dict[str, str]:
    peers = json.loads(parse_env(ROOT / ".env")["LAS_A2A_PEERS"])
    tokens = {meta["worker"]: token for token, meta in peers.items()}
    missing = {"codex", "dsh"} - tokens.keys()
    if missing:
        raise RuntimeError(f"missing peer token for: {', '.join(sorted(missing))}")
    return tokens


TOKENS = _peer_tokens()


def call(method: str, params: dict, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        BASE + "/a2a", headers=headers,
        data=json.dumps({
            "jsonrpc": "2.0", "id": "production-smoke",
            "method": method, "params": params,
        }).encode(),
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def get(path: str, token: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(BASE + path)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def send(text: str, worker: str, **metadata) -> dict:
    status, response = call("SendMessage", {"message": {
        "role": "user",
        "parts": [{"text": text, "mediaType": "text/plain"}],
        "metadata": metadata,
    }}, TOKENS[worker])
    if status != 200 or "error" in response:
        raise RuntimeError(f"{worker} SendMessage failed: {response}")
    return response["result"]["task"]


def wait_terminal(task_id: str, worker: str, timeout: float = 600) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, response = call(
            "tasks/get", {"id": task_id}, TOKENS[worker])
        if status != 200 or "error" in response:
            raise RuntimeError(f"tasks/get failed: {response}")
        task = response["result"]
        state = task["status"]["state"]
        if state in TERMINAL:
            return task
        if state == "input-required":
            raise RuntimeError(
                f"unexpected native approval during read-only smoke: {task_id}")
        time.sleep(2)
    raise TimeoutError(f"task did not finish within {timeout}s: {task_id}")


def main() -> int:
    assert get("/.well-known/agent-card.json")[0] == 401
    status, card = get(
        "/.well-known/agent-card.json", TOKENS["codex"])
    assert status == 200 and card["name"] == "agenthub-orchestrator"
    print("PASS: orchestrator authentication and agent card")
    return 0


def _run() -> int:
    # Keep the route-conflict check outside send(), where an error is expected.
    status, response = call("SendMessage", {"message": {
        "role": "user",
        "parts": [{"text": "查询路由安全状态", "mediaType": "text/plain"}],
        "metadata": {"agent": "dsh"},
    }}, TOKENS["codex"])
    assert status == 200 and response["error"]["code"] == -32602
    print("PASS: peer identity rejects forged worker routing")

    completed: dict[str, dict] = {}
    prompts = {
        "codex": "总结此标记并只回复 CODEX_PRODUCTION_SMOKE_OK："
                 "CODEX_PRODUCTION_SMOKE_OK",
        "dsh": "总结此标记并只回复 DSH_PRODUCTION_SMOKE_OK："
               "DSH_PRODUCTION_SMOKE_OK",
    }
    for worker, prompt in prompts.items():
        task = send(prompt, worker)
        final = wait_terminal(task["id"], worker)
        assert final["status"]["state"] == "completed", final
        assert final["metadata"]["assigned_to"] == worker
        completed[worker] = final
        print(f"PASS: {worker} production task completed: {task['id']}")

    status, cross_peer = call(
        "tasks/get", {"id": completed["codex"]["id"]}, TOKENS["dsh"])
    assert status == 200 and cross_peer["result"]["id"] == completed["codex"]["id"]
    print("PASS: cross-peer task status remains traceable")

    pending = send(
        "写入 production-smoke-must-not-exist.txt", "codex")
    assert pending["status"]["state"] == "input-required", pending
    status, rejected = call(
        "tasks/reject", {"id": pending["id"]}, TOKENS["codex"])
    assert status == 200
    assert rejected["result"]["status"]["state"] == "canceled"
    print("PASS: write task stayed gated and was rejected without delegation")
    return 0


if __name__ == "__main__":
    # main() performs the unauthenticated/authenticated card checks; _run()
    # performs expected-error routing and task lifecycle checks.
    main()
    raise SystemExit(_run())
