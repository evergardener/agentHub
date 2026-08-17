"""Fake Worker 的 Agent Card（设计文档 §3.3 / §10）。"""

from __future__ import annotations


def agent_card(base_url: str) -> dict:
    return {
        "name": "fake-worker",
        "description": "Phase 1 PoC 用假 Worker：接收任务、等待 1 秒、产出 artifact、返回结果。",
        "url": base_url,
        "version": "0.1.0",
        "protocolVersion": "0.3",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text", "file"],
        "skills": [
            {
                "id": "echo",
                "name": "Echo",
                "description": "回显任务目标并生成 artifact",
                "tags": ["smoke_test"],
            },
            {
                "id": "smoke_test",
                "name": "Smoke Test",
                "description": "端到端链路验证",
                "tags": ["smoke_test"],
            },
        ],
    }
