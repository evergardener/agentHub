"""WebUI task-detail artifact classification integration coverage."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_task_detail_keeps_runtime_records_but_marks_them_non_deliverable(
        tmp_path, monkeypatch):
    test_db_url = f"sqlite:///{tmp_path / 'webui.db'}"
    # Runtime configuration is PostgreSQL-only; this is an explicit offline
    # presentation fixture rather than a supported deployment backend.
    monkeypatch.setattr(
        "common.config.database_url", lambda: test_db_url)
    workspace = tmp_path / "ws"
    monkeypatch.setenv("LAS_WORKSPACE", str(workspace))
    for name in ("LAS_WEBUI_TOKENS", "LAS_WEBUI_SESSION_SECRET",
                 "LAS_WEBUI_REQUIRE_AUTH", "LAS_WEBUI_COOKIE_SECURE"):
        monkeypatch.delenv(name, raising=False)

    from common.models import TaskStatus
    from orchestrator import state_store
    from state.db import init_db

    conn = init_db(tmp_path / "webui.db")
    state_store.create_task(
        conn, task_id="T-artifacts", objective="检查本地容器",
        created_by="test", assigned_to="dsh", status=TaskStatus.COMPLETED,
    )
    runtime_dir = workspace / "tasks" / "T-artifacts" / "artifacts"
    runtime_dir.mkdir(parents=True)
    history = runtime_dir / "dsh-history.json"
    answer = runtime_dir / "last-message.md"
    output = runtime_dir / "workspace" / "report.md"
    output.parent.mkdir()
    history.write_text("{}", encoding="utf-8")
    answer.write_text("检查完成", encoding="utf-8")
    output.write_text("业务结果", encoding="utf-8")
    state_store.add_artifact(
        conn, task_id="T-artifacts", agent_id="dsh",
        name="dsh-history.json", path=str(history), sha256="0" * 64,
        artifact_type="log", commit=False,
    )
    state_store.add_artifact(
        conn, task_id="T-artifacts", agent_id="dsh",
        name="last-message.md", path=str(answer), sha256="0" * 64,
        artifact_type="report", commit=False,
    )
    state_store.add_artifact(
        conn, task_id="T-artifacts", agent_id="dsh",
        name="workspace/report.md", path=str(output), sha256="0" * 64,
        artifact_type="file", commit=False,
    )
    conn.commit()
    conn.close()

    from webui.server import create_app

    with TestClient(create_app()) as client:
        response = client.get("/api/tasks/T-artifacts")
    assert response.status_code == 200
    artifacts = {item["name"]: item for item in response.json()["artifacts"]}
    assert artifacts["dsh-history.json"]["is_deliverable"] is False
    assert artifacts["last-message.md"]["is_deliverable"] is False
    assert artifacts["workspace/report.md"]["is_deliverable"] is True
