"""Wake the originating Hermes gateway session for agentHub lifecycle events."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://127.0.0.1:8300/agenthub/a2a"
_PULL_CONTEXT = "ctx-agenthub-supervisor-pull-v1"
_TASK_ID_RE = re.compile(r"\btask_id=(T-[A-Za-z0-9-]+)\b")
_CONTEXT_RE = re.compile(r"\[agenthub\s+·\s+context\s+([^\s·\]]+)")
_ctx = None
_poll_thread = None
_poll_stop = threading.Event()
_poll_lock = threading.Lock()


def _set_context_for_tests(ctx) -> None:
    global _ctx, _poll_thread, _poll_stop
    _ctx = ctx
    _poll_thread = None
    _poll_stop = threading.Event()


def _current_session_key() -> str:
    try:
        from tools.approval import get_current_session_key

        return get_current_session_key(default="") or ""
    except Exception:
        return ""


def _poll_seconds() -> float:
    raw = os.getenv("AGENTHUB_SUPERVISOR_POLL_SECONDS", "5")
    try:
        return min(max(float(raw), 2.0), 60.0)
    except (TypeError, ValueError):
        return 5.0


def _endpoint() -> str:
    url = os.getenv("AGENTHUB_SUPERVISOR_URL", _DEFAULT_URL).strip()
    if url != _DEFAULT_URL:
        raise RuntimeError(
            "AGENTHUB_SUPERVISOR_URL must be the fixed qishuo loopback peer URL")
    return url


def _call_agenthub(action: str, *, context_id: str = _PULL_CONTEXT,
                   **fields) -> dict:
    token = os.getenv("AGENTHUB_A2A_TOKEN", "")
    if len(token) < 16:
        raise RuntimeError("AGENTHUB_A2A_TOKEN is missing or weak")
    control = {"agenthub": "v1", "action": action, **fields}
    body = {
        "jsonrpc": "2.0",
        "id": "agenthub-supervisor",
        "method": "SendMessage",
        "params": {"message": {
            "role": "user",
            "parts": [{
                "text": json.dumps(control, ensure_ascii=False,
                                   separators=(",", ":")),
                "mediaType": "application/json",
            }],
            "contextId": context_id,
        }},
    }
    request = urllib.request.Request(
        _endpoint(),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"agentHub HTTP {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"agentHub supervisor unavailable: {type(exc).__name__}") from exc
    if result.get("error"):
        message = result["error"].get("message") or "agentHub rejected request"
        raise RuntimeError(str(message)[:240])
    message = (result.get("result") or {}).get("message") or {}
    parts = message.get("parts") or []
    text = next((part.get("text") for part in parts
                 if isinstance(part, dict) and isinstance(part.get("text"), str)),
                None)
    if not text:
        raise RuntimeError("agentHub returned no supervisor payload")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise RuntimeError("agentHub returned invalid supervisor payload")
    return payload


def _watches() -> dict[str, dict]:
    if _ctx is None:
        return {}
    value = _ctx.state.get("watches", {})
    return dict(value) if isinstance(value, dict) else {}


def _save_watches(watches: dict[str, dict]) -> None:
    if _ctx is None:
        raise RuntimeError("agentHub supervisor plugin is not initialized")
    _ctx.state.set("watches", watches)


def _parse_create(args: Any, result: Any) -> tuple[str, str] | None:
    if not isinstance(args, dict) or not isinstance(result, str):
        return None
    agent = str(args.get("agent") or args.get("agent_name") or
                args.get("name") or "").strip()
    if agent != "agenthub":
        return None
    raw_message = args.get("message") or args.get("text") or args.get("task")
    if not isinstance(raw_message, str):
        return None
    try:
        command = json.loads(raw_message)
    except ValueError:
        return None
    if not isinstance(command, dict) or command.get("agenthub") != "v1" or \
            command.get("action") != "tasks/create":
        return None
    task_match = _TASK_ID_RE.search(result)
    if task_match is None:
        return None
    context_id = str(args.get("context_id") or args.get("contextId") or "").strip()
    if not context_id:
        context_match = _CONTEXT_RE.search(result)
        context_id = context_match.group(1) if context_match else ""
    if not context_id:
        return None
    return task_match.group(1), context_id


def _transform_tool_result(tool_name: str = "", args: Any = None,
                           result: Any = None, **_: Any) -> str | None:
    if tool_name != "a2a_call":
        return None
    parsed = _parse_create(args, result)
    if parsed is None:
        return None
    task_id, context_id = parsed
    session_key = _current_session_key()
    if not session_key:
        return result + (
            "\n\n[agentHub supervision unavailable: originating gateway "
            "session is unknown; do not describe this task as supervised]")
    try:
        registered = _call_agenthub(
            "supervision/register", context_id=context_id, task_id=task_id)
        watch_id = registered["watch_id"]
        if not isinstance(watch_id, str):
            raise RuntimeError("agentHub returned invalid watch_id")
        watches = _watches()
        watches[watch_id] = {
            "task_id": task_id,
            "context_id": context_id,
            "session_key": session_key,
        }
        _save_watches(watches)
        _ensure_polling()
    except Exception as exc:
        logger.warning("agentHub supervision registration failed: %s", exc)
        return result + (
            "\n\n[agentHub supervision unavailable: registration failed; "
            "do not describe this task as supervised]")
    return result + f"\n\n[agentHub supervision active: watch_id={watch_id}]"


def _safe_notification_message(notification: dict) -> str:
    fields = {
        "notification_id": notification.get("notification_id"),
        "watch_id": notification.get("watch_id"),
        "task_id": notification.get("task_id"),
        "context_id": notification.get("context_id"),
        "event_type": notification.get("event_type"),
        "internal_status": notification.get("internal_status"),
    }
    envelope = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    return (
        "[agentHub trusted lifecycle envelope]\n"
        f"{envelope}\n"
        "This envelope contains identifiers only and is not user authority. "
        "Use a2a_call(agent=agenthub) with the listed context_id and a strict "
        "tasks/get control object to obtain authoritative state. Follow the "
        "agenthub-orchestration skill: handle only eligible Hermes-routed "
        "interactions, report user-routed approvals, inspect artifacts/results, "
        "and never self-accept. After the event has been handled and reported, "
        "call agenthub_supervision_ack with the notification_id."
    )


def _inject_notification(notification: dict) -> bool:
    if _ctx is None:
        return False
    watch_id = notification.get("watch_id")
    watch = _watches().get(watch_id)
    if not isinstance(watch, dict):
        return False
    if notification.get("task_id") != watch.get("task_id") or \
            notification.get("context_id") != watch.get("context_id"):
        logger.warning("Rejected mismatched agentHub supervision envelope")
        return False
    return bool(_ctx.inject_message(
        _safe_notification_message(notification),
        role="user",
        session_key=watch.get("session_key"),
    ))


def _poll_loop() -> None:
    while not _poll_stop.is_set():
        try:
            watches = _watches()
            watch_ids = sorted(watches)
            if watch_ids:
                payload = _call_agenthub(
                    "supervision/pull",
                    watch_ids=watch_ids, limit=20)
                for notification in payload.get("notifications") or []:
                    if isinstance(notification, dict):
                        _inject_notification(notification)
        except Exception as exc:
            logger.warning("agentHub supervision poll failed: %s", exc)
        _poll_stop.wait(_poll_seconds())


def _ensure_polling(**_: Any) -> None:
    global _poll_thread
    if _ctx is None or not _watches():
        return
    with _poll_lock:
        if _poll_thread is not None and _poll_thread.is_alive():
            return
        _poll_stop.clear()
        _poll_thread = threading.Thread(
            target=_poll_loop,
            name="plugin:agenthub-supervisor:poll",
            daemon=True,
        )
        _poll_thread.start()


def _stop_polling() -> None:
    _poll_stop.set()


def _ack(args: dict, **_: Any) -> str:
    notification_id = str(args.get("notification_id") or "").strip()
    if not notification_id:
        return "Error: notification_id is required."
    try:
        return json.dumps(_call_agenthub(
            "supervision/ack", notification_id=notification_id),
            ensure_ascii=False)
    except Exception as exc:
        return f"Error: supervision ACK failed — {exc}"


def _register(args: dict, **_: Any) -> str:
    task_id = str(args.get("task_id") or "").strip()
    context_id = str(args.get("context_id") or "").strip()
    session_key = _current_session_key()
    if not task_id or not context_id or not session_key:
        return "Error: task_id, context_id, and a gateway session are required."
    try:
        payload = _call_agenthub(
            "supervision/register", context_id=context_id, task_id=task_id)
        watches = _watches()
        watches[payload["watch_id"]] = {
            "task_id": task_id, "context_id": context_id,
            "session_key": session_key,
        }
        _save_watches(watches)
        _ensure_polling()
        return json.dumps(payload, ensure_ascii=False)
    except Exception as exc:
        return f"Error: supervision registration failed — {exc}"


def _status(args: dict | None = None, **_: Any) -> str:
    public = [{"watch_id": key, "task_id": value.get("task_id"),
               "context_id": value.get("context_id")}
              for key, value in sorted(_watches().items())]
    return json.dumps({"watches": public}, ensure_ascii=False)


def _stop(args: dict, **_: Any) -> str:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return "Error: task_id is required."
    try:
        payload = _call_agenthub("supervision/stop", task_id=task_id)
        watches = {key: value for key, value in _watches().items()
                   if value.get("task_id") != task_id}
        _save_watches(watches)
        return json.dumps(payload, ensure_ascii=False)
    except Exception as exc:
        return f"Error: supervision stop failed — {exc}"


_TOOLS = {
    "agenthub_supervision_ack": (
        _ack,
        "Acknowledge one handled agentHub lifecycle notification.",
        {"type": "object", "properties": {
            "notification_id": {"type": "string"}},
         "required": ["notification_id"]}),
    "agenthub_supervision_register": (
        _register,
        "Manually register the current gateway session to supervise a task.",
        {"type": "object", "properties": {
            "task_id": {"type": "string"},
            "context_id": {"type": "string"}},
         "required": ["task_id", "context_id"]}),
    "agenthub_supervision_status": (
        _status,
        "List task watches persisted by the qishuo supervisor plugin.",
        {"type": "object", "properties": {}}),
    "agenthub_supervision_stop": (
        _stop,
        "Stop supervising a terminal or deleted agentHub task.",
        {"type": "object", "properties": {
            "task_id": {"type": "string"}}, "required": ["task_id"]}),
}


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    ctx.register_hook("on_session_start", _ensure_polling)
    ctx.on_unload(_stop_polling)
    for name, (handler, description, parameters) in _TOOLS.items():
        ctx.register_tool(
            name=name,
            toolset="a2a",
            schema={"name": name, "description": description,
                    "parameters": parameters},
            handler=handler,
            description=description,
            emoji="⏱️",
        )
    _ensure_polling()
