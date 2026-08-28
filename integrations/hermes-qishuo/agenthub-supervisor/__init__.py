"""Wake the originating Hermes gateway session for agentHub lifecycle events."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://127.0.0.1:8300/agenthub/a2a"
_PULL_CONTEXT = "ctx-agenthub-supervisor-pull-v1"
_TASK_ID_RE = re.compile(r"\btask_id=(T-[A-Za-z0-9-]+)\b")
_CONTEXT_RE = re.compile(r"\[agenthub\s+·\s+context\s+([^\s·\]]+)")
_ctx = None
_poll_thread = None
_poll_stop = threading.Event()
_poll_lock = threading.Lock()
_delivery_lock = threading.Lock()
_poll_surface = ""
_last_poll_skip_signature = None

# A watch registered from a CLI/TUI process has no durable Gateway route.  It
# may still be useful while that process is alive (PluginContext can enqueue a
# local input), but another process must never adopt it from the shared plugin
# state file.  A random process token is stronger than a PID, which can be
# reused after a CLI exits.
_PROCESS_OWNER_ID = f"pid-{os.getpid()}-{uuid4().hex[:16]}"
_CLI_SOURCES = frozenset({
    "cli", "tui", "desktop",
})
_NON_DELIVERING_SURFACES = frozenset({
    "api_server", "webhook", "msgraph_webhook", "kanban", "local", "codex",
    "tool",
})


def _set_context_for_tests(ctx) -> None:
    global _ctx, _poll_thread, _poll_stop, _poll_surface
    global _last_poll_skip_signature
    _ctx = ctx
    _poll_thread = None
    _poll_stop = threading.Event()
    _poll_surface = ""
    _last_poll_skip_signature = None


def _session_env(name: str) -> str:
    """Read the session context without making the plugin depend on Hermes."""
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env(name, "") or "").strip().lower()
    except Exception:
        return str(os.getenv(name, "") or "").strip().lower()


def _current_session_key() -> str:
    try:
        from tools.approval import get_current_session_key

        return get_current_session_key(default="") or ""
    except Exception:
        return ""


def _cli_ref_available() -> bool | None:
    """Return whether this PluginContext can enqueue an in-process CLI turn.

    Real PluginContext instances always have a manager.  Returning ``None``
    for a small test/stub context keeps unit tests independent of Hermes while
    production remains fail-closed when the manager explicitly has no CLI ref.
    """
    manager = getattr(_ctx, "_manager", None) if _ctx is not None else None
    if manager is None:
        return None
    return getattr(manager, "_cli_ref", None) is not None


def _session_surface(session_key: str = "") -> str:
    """Classify the current execution surface and its supported wake route."""
    key = session_key or _current_session_key()
    platform = _session_env("HERMES_SESSION_PLATFORM")
    source = _session_env("HERMES_SESSION_SOURCE")
    # hermes-web-ui runs a long-lived Python agent bridge.  It deliberately
    # reports source=tui for prompt compatibility, but its ``mt...`` session
    # is not an in-process CLI input queue.  The bridge owns a separate,
    # durable async-completion queue, so classify it before the TUI aliases.
    if platform == "agent_bridge":
        return "agent_bridge"
    if source in _CLI_SOURCES or platform in _CLI_SOURCES:
        return "cli"
    if source in _NON_DELIVERING_SURFACES or \
            platform in _NON_DELIVERING_SURFACES:
        return "unsupported"
    if _cli_ref_available() is True:
        return "cli"
    # Hermes Gateway routing keys are build_session_key() values with the
    # ``agent:`` namespace.  Bare CLI/worker keys (for example ``mt...``) are
    # process-local and must not be advertised as durable supervision.
    if key and key.startswith("agent:"):
        return "gateway"
    if key and not platform and not source:
        return "cli"
    if platform or source:
        return "gateway"
    return "unknown"


def _is_durable_route(surface: str, session_key: str) -> bool:
    """Return whether Hermes can recover this route after a process restart."""
    return (
        surface == "gateway" and session_key.startswith("agent:")
    ) or (
        surface == "agent_bridge" and bool(session_key)
    )


def _watch_record(*, task_id: str, context_id: str, session_key: str,
                  surface: str) -> dict:
    durable = _is_durable_route(surface, session_key)
    return {
        "task_id": task_id,
        "context_id": context_id,
        "session_key": session_key,
        "owner_mode": surface,
        "owner_instance_id": "" if durable else _PROCESS_OWNER_ID,
        "durable": durable,
    }


def _watch_surface(watch: dict) -> str:
    value = str(watch.get("owner_mode") or "").strip().lower()
    if value in {"gateway", "cli", "agent_bridge"}:
        return value
    # Legacy state had no ownership metadata.  Only canonical Gateway keys can
    # be classified without the original session environment.  In particular,
    # never upgrade a bare ``mt...`` watch to agent_bridge merely because a new
    # bridge process is currently loading it: that would adopt and write into a
    # pre-upgrade user session.  Such watches remain unowned process-only state
    # and require an explicit fresh registration.
    session_key = str(watch.get("session_key") or "")
    return "gateway" if session_key.startswith("agent:") else "cli"


def _watch_is_durable(watch: dict) -> bool:
    value = watch.get("durable")
    if isinstance(value, bool):
        return value
    return _is_durable_route(
        _watch_surface(watch), str(watch.get("session_key") or ""))


def _owned_watches(surface: str | None = None) -> dict[str, dict]:
    """Return only watches this process is allowed to pull."""
    selected_surface = surface or _poll_surface
    if selected_surface not in {"gateway", "cli", "agent_bridge"}:
        return {}
    result = {}
    for watch_id, watch in _watches().items():
        if not isinstance(watch, dict):
            continue
        watch_surface = _watch_surface(watch)
        # The long-lived Gateway is the restart-safe relay for Studio-owned
        # watches.  Agent-bridge workers are created lazily, so after a Studio
        # restart there may be no profile worker alive to poll agentHub.  The
        # Gateway can still publish the wake through Hermes' durable native
        # completion queue; the Studio bridge consumes that queue separately.
        gateway_bridge_relay = (
            selected_surface == "gateway"
            and watch_surface == "agent_bridge"
            and _watch_is_durable(watch)
            and _agent_bridge_delivery_available()
        )
        if watch_surface != selected_surface and not gateway_bridge_relay:
            continue
        if _watch_is_durable(watch):
            result[watch_id] = watch
            continue
        if watch.get("owner_instance_id") == _PROCESS_OWNER_ID:
            result[watch_id] = watch
    return result


def _agent_bridge_delivery_available() -> bool:
    """Feature-detect Hermes' durable WebUI completion dispatcher."""
    try:
        from tools.async_delegation import dispatch_async_delegation

        return callable(dispatch_async_delegation)
    except Exception:
        return False


def _is_gateway_process() -> bool:
    """Identify the documented Hermes ``gateway run`` host command."""
    args = [str(value).strip().lower() for value in sys.argv[1:]]
    return any(
        args[index:index + 2] == ["gateway", "run"]
        for index in range(max(0, len(args) - 1))
    )


def _native_async_dispatch(**kwargs) -> dict:
    """Dispatch through Hermes' public durable async-completion API."""
    from tools.async_delegation import dispatch_async_delegation

    return dispatch_async_delegation(**kwargs)


def _native_delivery_is_pending(delegation_id: str) -> bool:
    """Return whether Hermes still owns an undelivered native completion."""
    try:
        from tools.async_delegation import get_durable_delegation

        record = get_durable_delegation(delegation_id)
    except Exception:
        # Losing access to the durable ledger must not create duplicate turns.
        # The existing record remains visible through supervision/status and a
        # bridge restart can still restore the native completion.
        return True
    if not isinstance(record, dict):
        return False
    return record.get("delivery_state") == "pending" or record.get("state") in {
        "running", "finalizing",
    }


def _delivery_surface_available(watch: dict) -> bool:
    """Preflight delivery before pulling, so a failed route keeps its row pending."""
    surface = _watch_surface(watch)
    if surface == "agent_bridge":
        return _agent_bridge_delivery_available()
    manager = getattr(_ctx, "_manager", None) if _ctx is not None else None
    if manager is None:
        # Test contexts provide inject_message directly.  Hermes PluginContext
        # always has a manager, so this does not weaken production fail-closed
        # behavior.
        return _ctx is not None
    if surface == "cli":
        return getattr(manager, "_cli_ref", None) is not None
    if surface == "gateway":
        return bool(getattr(manager, "has_gateway_message_injector", False))
    return False


def _delivery_label(watch: dict) -> str:
    surface = _watch_surface(watch)
    if surface == "agent_bridge" and _watch_is_durable(watch):
        return "agent-bridge-durable"
    if _watch_is_durable(watch):
        return "gateway-durable"
    if surface == "cli":
        return "cli-process"
    if surface == "gateway":
        return "gateway-process"
    return "unavailable"


def _poll_seconds() -> float:
    raw = os.getenv("AGENTHUB_SUPERVISOR_POLL_SECONDS", "5")
    try:
        return min(max(float(raw), 2.0), 60.0)
    except (TypeError, ValueError):
        return 5.0


def _redelivery_holdoff_seconds() -> float:
    """Bound duplicate native turns while Hermes is handling a delivered wake."""
    raw = os.getenv("AGENTHUB_SUPERVISOR_REDELIVERY_SECONDS", "900")
    try:
        return min(max(float(raw), 300.0), 3600.0)
    except (TypeError, ValueError):
        return 900.0


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


def _deliveries() -> dict[str, dict]:
    if _ctx is None:
        return {}
    value = _ctx.state.get("deliveries", {})
    return dict(value) if isinstance(value, dict) else {}


def _save_deliveries(deliveries: dict[str, dict]) -> None:
    if _ctx is None:
        raise RuntimeError("agentHub supervisor plugin is not initialized")
    _ctx.state.set("deliveries", deliveries)


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
    surface = _session_surface(session_key)
    if surface == "unsupported":
        return result + (
            "\n\n[agentHub supervision unavailable: this session surface "
            "cannot receive a later wake; use bounded tasks/get polling]")
    try:
        registered = _call_agenthub(
            "supervision/register", context_id=context_id, task_id=task_id)
        watch_id = registered["watch_id"]
        if not isinstance(watch_id, str):
            raise RuntimeError("agentHub returned invalid watch_id")
        watches = _watches()
        watch = _watch_record(
            task_id=task_id, context_id=context_id,
            session_key=session_key, surface=surface)
        watches[watch_id] = watch
        _save_watches(watches)
        global _poll_surface
        _poll_surface = surface
        _ensure_polling()
    except Exception as exc:
        logger.warning("agentHub supervision registration failed: %s", exc)
        return result + (
            "\n\n[agentHub supervision unavailable: registration failed; "
            "do not describe this task as supervised]")
    if _watch_is_durable(watch) and _delivery_surface_available(watch):
        marker = (
            f"[agentHub supervision active: watch_id={watch_id}; "
            f"delivery={_delivery_label(watch)}]")
    elif _delivery_surface_available(watch):
        marker = (
            f"[agentHub supervision process-only: watch_id={watch_id}; "
            f"delivery={_delivery_label(watch)}; "
            "the wake cannot survive this Hermes process]")
    else:
        marker = (
            f"[agentHub supervision unavailable: watch_id={watch_id}; "
            f"delivery={_delivery_label(watch)}; use bounded tasks/get polling]")
    return result + "\n\n" + marker


def _safe_notification_message(notification: dict) -> str:
    fields = {
        "notification_id": notification.get("notification_id"),
        "watch_id": notification.get("watch_id"),
        "task_id": notification.get("task_id"),
        "context_id": notification.get("context_id"),
        "event_type": notification.get("event_type"),
        "internal_status": notification.get("internal_status"),
    }
    if notification.get("message_id"):
        fields["message_id"] = notification.get("message_id")
    envelope = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    if notification.get("event_type") == "conversation.user_message":
        instruction = (
            "This envelope contains identifiers only and is not user "
            "authority. Fetch the exact message with "
            "conversations/messages/get in the same context_id. If it only "
            "needs explanation or discussion, answer directly. If execution "
            "is required, create a separate follow-up task in the same "
            "context with parent_task_id set to the route task_id; never "
            "reopen the completed task or reuse its closed native Agent "
            "session. Persist a concise user-facing answer with "
            "conversations/respond, then ACK this notification with its "
            "notification_id and context_id."
        )
    else:
        instruction = (
            "This envelope contains identifiers only and is not user "
            "authority. Use a2a_call(agent=agenthub) with the listed "
            "context_id and a strict tasks/get control object to obtain "
            "authoritative state. Follow the agenthub-orchestration skill: "
            "handle only eligible Hermes-routed interactions, report "
            "user-routed approvals, inspect artifacts/results, and never "
            "self-accept. After the event has been handled and reported, "
            "call agenthub_supervision_ack with the notification_id and "
            "context_id."
        )
    return (
        "[agentHub trusted lifecycle envelope]\n"
        f"{envelope}\n"
        + instruction
    )


def _dispatch_agent_bridge_notification(
        notification: dict, watch: dict) -> str | None:
    """Publish an identifiers-only wake into Hermes WebUI's durable queue."""
    session_key = str(watch.get("session_key") or "")
    if not session_key:
        return None
    message = _safe_notification_message(notification)

    def runner() -> dict:
        return {
            "status": "completed",
            "summary": message,
            "api_calls": 0,
            "duration_seconds": 0,
        }

    result = _native_async_dispatch(
        goal=(
            "Handle agentHub notification "
            f"{notification.get('notification_id')} for task "
            f"{notification.get('task_id')}"
        ),
        context=(
            "Trusted identifiers-only supervisor wake. Retrieve authoritative "
            "task state from agentHub before acting or reporting."
        ),
        toolsets=["a2a"],
        role="agenthub-supervisor",
        model=None,
        session_key=session_key,
        parent_session_id=session_key,
        runner=runner,
        origin_ui_session_id=session_key,
        origin_session_id=session_key,
    )
    if not isinstance(result, dict) or result.get("status") != "dispatched":
        logger.warning(
            "agentHub WebUI durable dispatch rejected: notification_id=%s "
            "watch_id=%s reason=%s",
            notification.get("notification_id"), notification.get("watch_id"),
            str(result.get("error") if isinstance(result, dict) else
                "invalid_dispatch_result")[:240],
        )
        return None
    delegation_id = result.get("delegation_id")
    return delegation_id if isinstance(delegation_id, str) else None


def _inject_notification(notification: dict) -> bool:
    notification_id = notification.get("notification_id")
    watch_id = notification.get("watch_id")
    if _ctx is None:
        logger.warning(
            "agentHub supervision injection failed: notification_id=%s "
            "watch_id=%s reason=no_plugin_context",
            notification_id, watch_id)
        return False
    watch = _watches().get(watch_id)
    if not isinstance(watch, dict):
        logger.warning(
            "agentHub supervision injection failed: notification_id=%s "
            "watch_id=%s reason=unknown_watch",
            notification_id, watch_id)
        return False
    if notification.get("task_id") != watch.get("task_id") or \
            notification.get("context_id") != watch.get("context_id"):
        logger.warning(
            "agentHub supervision injection failed: notification_id=%s "
            "watch_id=%s task_id=%s context_id=%s session_key=%s surface=%s "
            "reason=mismatched_envelope",
            notification_id, watch_id, notification.get("task_id"),
            notification.get("context_id"), watch.get("session_key"),
            _watch_surface(watch))
        return False
    if not _delivery_surface_available(watch):
        logger.warning(
            "agentHub supervision injection failed: notification_id=%s "
            "watch_id=%s task_id=%s context_id=%s session_key=%s surface=%s "
            "reason=delivery_surface_unavailable",
            notification_id, watch_id, watch.get("task_id"),
            watch.get("context_id"), watch.get("session_key"),
            _watch_surface(watch))
        return False
    if _watch_surface(watch) == "agent_bridge":
        # The agentHub outbox deliberately redelivers until Hermes ACKs after
        # handling.  Persist the native delegation id immediately so repeated
        # pulls do not create duplicate WebUI turns.
        with _delivery_lock:
            deliveries = _deliveries()
            prior = deliveries.get(notification_id)
            if isinstance(prior, dict):
                delegation_id = str(prior.get("delegation_id") or "")
                if delegation_id and _native_delivery_is_pending(delegation_id):
                    return True
                dispatched_at = prior.get("dispatched_at")
                if (isinstance(dispatched_at, (int, float))
                        and time.time() - float(dispatched_at)
                        < _redelivery_holdoff_seconds()):
                    # The native completion was delivered, but the Hermes turn
                    # may still be fetching state, creating a follow-up task,
                    # or writing its response.  Keep renewing the agentHub
                    # lease without starting a concurrent duplicate turn.
                    return True
                # A native completion was already delivered but Hermes did not
                # ACK the agentHub outbox (for example its follow-up turn
                # failed).  Permit a new durable wake instead of suppressing
                # retries forever.
                deliveries.pop(notification_id, None)
            try:
                delegation_id = _dispatch_agent_bridge_notification(
                    notification, watch)
            except Exception as exc:
                logger.warning(
                    "agentHub supervision injection failed: notification_id=%s "
                    "watch_id=%s task_id=%s context_id=%s session_key=%s "
                    "surface=agent_bridge reason=dispatch_exception "
                    "error_type=%s",
                    notification_id, watch_id, watch.get("task_id"),
                    watch.get("context_id"), watch.get("session_key"),
                    type(exc).__name__,
                )
                return False
            if not delegation_id:
                return False
            deliveries[str(notification_id)] = {
                "delegation_id": delegation_id,
                "watch_id": watch_id,
                "task_id": watch.get("task_id"),
                "session_key": watch.get("session_key"),
                "dispatched_at": time.time(),
            }
            _save_deliveries(deliveries)
        return True
    try:
        accepted = bool(_ctx.inject_message(
            _safe_notification_message(notification),
            role="user",
            session_key=watch.get("session_key"),
        ))
    except Exception as exc:
        logger.warning(
            "agentHub supervision injection failed: notification_id=%s "
            "watch_id=%s task_id=%s context_id=%s session_key=%s surface=%s "
            "reason=inject_exception error_type=%s",
            notification_id, watch_id, watch.get("task_id"),
            watch.get("context_id"), watch.get("session_key"),
            _watch_surface(watch), type(exc).__name__)
        return False
    if not accepted:
        logger.warning(
            "agentHub supervision injection failed: notification_id=%s "
            "watch_id=%s task_id=%s context_id=%s session_key=%s surface=%s "
            "reason=inject_rejected",
            notification_id, watch_id, watch.get("task_id"),
            watch.get("context_id"), watch.get("session_key"),
            _watch_surface(watch))
    return accepted


def _poll_loop() -> None:
    global _last_poll_skip_signature
    while not _poll_stop.is_set():
        try:
            watches = _owned_watches()
            watch_ids = sorted(watches)
            if watch_ids:
                eligible_ids = [
                    watch_id for watch_id in watch_ids
                    if _delivery_surface_available(watches[watch_id])
                ]
                skipped_ids = [
                    watch_id for watch_id in watch_ids
                    if watch_id not in eligible_ids
                ]
                if skipped_ids:
                    signature = (_poll_surface, tuple(skipped_ids))
                    if signature != _last_poll_skip_signature:
                        logger.warning(
                            "agentHub supervision poll skipped watches: "
                            "surface=%s watch_ids=%s reason="
                            "delivery_surface_unavailable",
                            _poll_surface, skipped_ids)
                        _last_poll_skip_signature = signature
                if not eligible_ids:
                    _poll_stop.wait(_poll_seconds())
                    continue
                _last_poll_skip_signature = None
                payload = _call_agenthub(
                    "supervision/pull",
                    watch_ids=eligible_ids, limit=20)
                for notification in payload.get("notifications") or []:
                    if isinstance(notification, dict):
                        _inject_notification(notification)
        except Exception as exc:
            logger.warning(
                "agentHub supervision poll failed: surface=%s watch_ids=%s: %s",
                _poll_surface, sorted(_owned_watches()), exc)
        _poll_stop.wait(_poll_seconds())


def _ensure_polling(surface: str | None = None, **_: Any) -> None:
    global _poll_thread, _poll_surface
    resolved_surface = surface or _poll_surface or _session_surface()
    # Gateway plugin discovery happens before Hermes installs its live message
    # injector, so a cold start has no session key or surface marker yet.  The
    # native completion API is process-independent and durable; when it is
    # available, keep a lightweight Gateway relay alive even before any watch
    # exists.  It will observe profile-state updates made later by a Studio
    # worker and can wake that worker through the native completion queue.
    if resolved_surface not in {"gateway", "cli", "agent_bridge"} and \
            _is_gateway_process() and _agent_bridge_delivery_available():
        resolved_surface = "gateway"
    if resolved_surface not in {"gateway", "cli", "agent_bridge"}:
        return
    _poll_surface = resolved_surface
    if _ctx is None:
        return
    if resolved_surface != "gateway" and not _owned_watches(resolved_surface):
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
    if _ctx is None:
        return
    try:
        watches = _watches()
        ephemeral = {
            key: value for key, value in watches.items()
            if isinstance(value, dict)
            and not _watch_is_durable(value)
            and value.get("owner_instance_id") == _PROCESS_OWNER_ID
        }
        if ephemeral:
            logger.warning(
                "agentHub supervision process-only watches expired with "
                "Hermes process: watch_ids=%s",
                sorted(ephemeral),
            )
            stopped = set()
            for task_id in sorted({
                    str(value.get("task_id") or "")
                    for value in ephemeral.values()
                    if value.get("task_id")
            }):
                try:
                    _call_agenthub("supervision/stop", task_id=task_id)
                    stopped.add(task_id)
                except Exception as exc:
                    logger.warning(
                        "agentHub supervision process-only watch stop failed: "
                        "task_id=%s reason=%s",
                        task_id, type(exc).__name__,
                    )
            _save_watches({
                key: value for key, value in watches.items()
                if key not in ephemeral
                or str(value.get("task_id") or "") not in stopped
            })
    except Exception as exc:
        logger.warning(
            "agentHub supervision process-only watch cleanup failed: %s",
            type(exc).__name__,
        )


def _ack(args: dict, **_: Any) -> str:
    notification_id = str(args.get("notification_id") or "").strip()
    context_id = str(args.get("context_id") or "").strip()
    if not notification_id or not context_id:
        return "Error: notification_id and context_id are required."
    try:
        payload = _call_agenthub(
            "supervision/ack", context_id=context_id,
            notification_id=notification_id)
        with _delivery_lock:
            deliveries = _deliveries()
            if notification_id in deliveries:
                deliveries.pop(notification_id, None)
                _save_deliveries(deliveries)
        return json.dumps(payload, ensure_ascii=False)
    except Exception as exc:
        return f"Error: supervision ACK failed — {exc}"


def _register(args: dict, **_: Any) -> str:
    task_id = str(args.get("task_id") or "").strip()
    context_id = str(args.get("context_id") or "").strip()
    session_key = _current_session_key()
    if not task_id or not context_id or not session_key:
        return "Error: task_id, context_id, and a session are required."
    surface = _session_surface(session_key)
    if surface == "unsupported":
        return "Error: this session surface cannot receive a supervision wake."
    try:
        payload = _call_agenthub(
            "supervision/register", context_id=context_id, task_id=task_id)
        watch_id = payload.get("watch_id")
        if not isinstance(watch_id, str) or not watch_id:
            raise RuntimeError("agentHub returned invalid watch_id")
        watches = _watches()
        watch = _watch_record(
            task_id=task_id, context_id=context_id,
            session_key=session_key, surface=surface)
        watches[watch_id] = watch
        _save_watches(watches)
        global _poll_surface
        _poll_surface = surface
        _ensure_polling()
        return json.dumps({
            **payload,
            "delivery": _delivery_label(watch),
            "durable": _watch_is_durable(watch),
        }, ensure_ascii=False)
    except Exception as exc:
        return f"Error: supervision registration failed — {exc}"


def _status(args: dict | None = None, **_: Any) -> str:
    public = []
    for key, value in sorted(_watches().items()):
        if not isinstance(value, dict):
            continue
        surface = _watch_surface(value)
        durable = _watch_is_durable(value)
        owned_here = durable or value.get("owner_instance_id") == _PROCESS_OWNER_ID
        public.append({
            "watch_id": key,
            "task_id": value.get("task_id"),
            "context_id": value.get("context_id"),
            "owner_mode": surface,
            "delivery": _delivery_label(value),
            "durable": durable,
            "owned_by_current_process": owned_here,
        })
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
            "notification_id": {"type": "string"},
            "context_id": {"type": "string"}},
         "required": ["notification_id", "context_id"]}),
    "agenthub_supervision_register": (
        _register,
        "Register the current Hermes session to supervise a task while its "
        "delivery surface remains available.",
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
