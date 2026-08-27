"""Contract checks for the Hermes agentHub orchestration skill."""

from pathlib import Path


SKILL = (
    Path(__file__).resolve().parents[2]
    / "integrations"
    / "hermes-qishuo"
    / "agenthub-orchestration"
    / "SKILL.md"
)


def test_skill_documents_native_interaction_get_and_respond_contracts():
    text = SKILL.read_text(encoding="utf-8")

    assert (
        '{"agenthub":"v1","action":"interactions/get",'
        '"interaction_id":"INT-..."}'
    ) in text
    assert '"action":"interactions/respond"' in text
    assert '"outcome":"allowed-once"' in text
    assert '"awaiting_hermes":true' in text
    assert '"awaiting_user":false' in text


def test_skill_keeps_task_approval_separate_from_native_interactions():
    text = SKILL.read_text(encoding="utf-8")

    assert "Do not use `tasks/approve` or `tasks/reject` for a native" in text
    assert "`input_required_kind=delegation`" in text
    assert "exactly one" in text and "`interactions/respond`" in text


def test_skill_requires_explicit_read_capability_for_read_dispatch():
    text = SKILL.read_text(encoding="utf-8")

    assert '"access_mode":"read"' in text
    assert "creation-time dispatch intent" in text
    assert "never authorizes" in text and "write" in text
    assert "unknown/unparseable commands" in text
