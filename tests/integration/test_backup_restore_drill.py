"""Isolated real Docker backup/restore drill; never targets the main project."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

from common.control_plane_backup import create_backup, restore_backup

pytestmark = pytest.mark.skipif(
    os.environ.get("LAS_RUN_RESTORE_DRILL") != "1",
    reason="set LAS_RUN_RESTORE_DRILL=1 for isolated Docker restore drill",
)


def _run(*args: str, output: bool = False) -> str:
    result = subprocess.run(
        list(args), check=True, text=True,
        stdout=subprocess.PIPE if output else subprocess.DEVNULL)
    return result.stdout.strip() if output else ""


def test_isolated_postgres_nats_agent_data_workspace_restore(
        tmp_path, monkeypatch):
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    project = f"agenthub_restore_drill_{uuid.uuid4().hex[:10]}"
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", project)
    compose = tmp_path / "compose.yml"
    compose.write_text(textwrap.dedent("""
        services:
          postgres:
            image: postgres:17.11-alpine3.24@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73
            environment:
              POSTGRES_USER: agenthub
              POSTGRES_PASSWORD: drill-only-password
              POSTGRES_DB: agenthub
            volumes: [pg-data:/var/lib/postgresql/data]
            healthcheck:
              test: ["CMD-SHELL", "pg_isready -U agenthub -d agenthub"]
              interval: 1s
              timeout: 2s
              retries: 30
          nats:
            image: nats:2.11.17-alpine3.22@sha256:e4bf19f15fd3218814a4e3c9e0064e1334bd8aa20d5984b9f1a0afd084f8cc00
            command: ["-js", "-sd", "/data"]
            volumes: [nats-data:/data]
          state-writer:
            image: agenthub:latest
            command:
              - sh
              - -c
              - >-
                mkdir -p /data/workspace/runtime;
                trap 'exit 0' TERM INT;
                while :; do sleep 1 & wait $!; done
            volumes: [agent-data:/data]
        volumes:
          pg-data:
          nats-data:
          agent-data:
    """), encoding="utf-8")
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    workspace = tmp_path / "host-workspace"
    workspace.mkdir()
    (workspace / "artifact.txt").write_text("original", encoding="utf-8")
    try:
        _run("docker", "compose", "up", "-d", "--wait")
        _run("docker", "compose", "exec", "-T", "postgres", "psql",
             "-U", "agenthub", "-d", "agenthub", "-v", "ON_ERROR_STOP=1",
             "-c", "CREATE TABLE drill(value text); INSERT INTO drill VALUES ('original');")
        _run("docker", "compose", "exec", "-T", "nats", "sh", "-c",
             "printf original > /data/drill.txt")
        _run("docker", "compose", "exec", "-T", "state-writer", "sh", "-c",
             "printf original > /data/drill.txt")

        archive = create_backup(tmp_path / "backups", workspace)

        _run("docker", "compose", "exec", "-T", "postgres", "psql",
             "-U", "agenthub", "-d", "agenthub", "-v", "ON_ERROR_STOP=1",
             "-c", "UPDATE drill SET value='mutated';")
        _run("docker", "compose", "exec", "-T", "nats", "sh", "-c",
             "printf mutated > /data/drill.txt")
        _run("docker", "compose", "exec", "-T", "state-writer", "sh", "-c",
             "printf mutated > /data/drill.txt")
        (workspace / "artifact.txt").write_text("mutated", encoding="utf-8")

        result = restore_backup(
            archive, tmp_path / "safety", workspace)

        assert _run(
            "docker", "compose", "exec", "-T", "postgres", "psql",
            "-U", "agenthub", "-d", "agenthub", "-Atc",
            "SELECT value FROM drill;", output=True) == "original"
        assert _run("docker", "compose", "exec", "-T", "nats", "sh", "-c",
                    "cat /data/drill.txt", output=True) == "original"
        assert _run("docker", "compose", "exec", "-T", "state-writer",
                    "sh", "-c", "cat /data/drill.txt", output=True) == "original"
        assert (workspace / "artifact.txt").read_text() == "original"
        preserved = Path(result["preserved_workspace"])
        assert (preserved / "artifact.txt").read_text() == "mutated"
        assert Path(result["safety_backup"]).is_file()
    finally:
        try:
            _run("docker", "compose", "down", "-v", "--remove-orphans")
        finally:
            os.chdir(old_cwd)
