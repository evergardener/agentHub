"""Contract tests for the PostgreSQL response-draft API.

The unit fixture creates only the migration-017 table shape locally so the
state-machine contract can run without requiring a developer PostgreSQL
server.  Production creates this table exclusively through migrations_pg.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import collaboration_store
from state.db import init_db


@pytest.fixture
def stream_env(tmp_path):
    conn = init_db(tmp_path / "stream.db")
    mapping = collaboration_store.ensure_a2a_collaboration(
        conn, peer="qishuo", context_id="ctx-stream", objective="stream test")
    message = collaboration_store.append_user_message_to_hermes(
        conn,
        collaboration_id=mapping["collaboration_id"],
        user_id="user",
        content={"text": "请解释结果"},
    )
    # This is the migration-017 shape.  The migration itself is intentionally
    # PostgreSQL-only and is exercised by the PG integration suite.
    conn.execute(
        """CREATE TABLE conversation_stream_drafts (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            collaboration_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            peer TEXT NOT NULL,
            context_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'streaming',
            text_prefix TEXT NOT NULL DEFAULT '',
            last_seq INTEGER NOT NULL DEFAULT 0,
            abort_reason TEXT,
            response_message_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finalized_at TEXT,
            UNIQUE(peer, context_id, message_id)
        );"""
    )
    conn.commit()
    yield conn, mapping, message
    conn.close()


def test_stream_is_idempotent_bounded_and_finalized(stream_env):
    conn, mapping, message = stream_env
    start = collaboration_store.start_conversation_stream(
        conn, peer="qishuo", context_id="ctx-stream", message_id=message["id"],
        stream_id="STR-test")
    assert start["status"] == "streaming"
    assert start["seq"] == 0
    assert start["text"] == ""
    assert collaboration_store.start_conversation_stream(
        conn, peer="qishuo", context_id="ctx-stream", message_id=message["id"],
        stream_id="STR-test")["stream_id"] == "STR-test"

    update = collaboration_store.update_conversation_stream(
        conn, peer="qishuo", context_id="ctx-stream", stream_id="STR-test",
        seq=1, text="第一段")
    assert update["text"] == "第一段"
    assert collaboration_store.update_conversation_stream(
        conn, peer="qishuo", context_id="ctx-stream", stream_id="STR-test",
        seq=1, text="第一段")["seq"] == 1
    with pytest.raises(collaboration_store.StreamConflict, match="gap"):
        collaboration_store.update_conversation_stream(
            conn, peer="qishuo", context_id="ctx-stream", stream_id="STR-test",
            seq=3, text="第一段第三段")
    with pytest.raises(collaboration_store.StreamConflict, match="cumulative"):
        collaboration_store.update_conversation_stream(
            conn, peer="qishuo", context_id="ctx-stream", stream_id="STR-test",
            seq=2, text="不连续")

    finished = collaboration_store.finish_conversation_stream(
        conn, peer="qishuo", context_id="ctx-stream", stream_id="STR-test",
        seq=2, text="第一段完成")
    assert finished["status"] == "finished"
    with pytest.raises(collaboration_store.StreamConflict, match="late delta"):
        collaboration_store.update_conversation_stream(
            conn, peer="qishuo", context_id="ctx-stream", stream_id="STR-test",
            seq=3, text="第一段完成迟到")
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages;").fetchone()[0] == 1

    events = conn.execute(
        "SELECT event_type, payload_json FROM events"
        " WHERE event_type LIKE 'conversation.stream.%';").fetchall()
    assert events
    for event in events:
        payload = json.loads(event["payload_json"])
        assert "text" not in payload
        assert "delta" not in payload


def test_stream_abort_fails_pending_delivery_and_is_idempotent(stream_env):
    conn, _, message = stream_env
    start = collaboration_store.start_conversation_stream(
        conn, peer="qishuo", context_id="ctx-stream", message_id=message["id"],
        stream_id="STR-abort")
    aborted = collaboration_store.abort_conversation_stream(
        conn, peer="qishuo", context_id="ctx-stream",
        stream_id=start["stream_id"], reason="upstream stopped")
    assert aborted["status"] == "aborted"
    assert conn.execute(
        "SELECT delivery_status FROM conversation_messages WHERE id = ?;",
        (message["id"],),
    ).fetchone()[0] == "failed"
    assert collaboration_store.abort_conversation_stream(
        conn, peer="qishuo", context_id="ctx-stream",
        stream_id=start["stream_id"], reason="upstream stopped")["status"] == \
        "aborted"
    # The stream is only a presentation draft: a later authoritative
    # conversations/respond may recover the delivery and close the draft.
    response = collaboration_store.record_a2a_hermes_response(
        conn, peer="qishuo", context_id="ctx-stream", message_id=message["id"],
        text="authoritative response")
    assert response["message_type"] == "llm.assistant"
    replay = collaboration_store.record_a2a_hermes_response(
        conn, peer="qishuo", context_id="ctx-stream", message_id=message["id"],
        text="authoritative response")
    assert replay["id"] == response["id"]
    assert conn.execute(
        "SELECT delivery_status FROM conversation_messages WHERE id = ?;",
        (message["id"],),
    ).fetchone()[0] == "delivered"
    assert collaboration_store.get_conversation_stream(
        conn, peer="qishuo", context_id="ctx-stream",
        stream_id=start["stream_id"])["status"] == "finished"
    with pytest.raises(collaboration_store.StreamConflict, match="late delta"):
        collaboration_store.update_conversation_stream(
            conn, peer="qishuo", context_id="ctx-stream",
            stream_id=start["stream_id"], seq=1,
            text="authoritative response plus late delta")
