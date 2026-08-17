"""Codex Adapter 的 Agent Card。"""

from __future__ import annotations


def agent_card(base_url: str) -> dict:
    return {
        "name": "codex",
        "description": "Codex CLI Worker：代码实现、调试、重构、测试、代码评审。",
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
            {"id": "coding", "name": "Coding",
             "description": "代码实现", "tags": ["dev"]},
            {"id": "testing", "name": "Testing",
             "description": "编写并运行测试", "tags": ["dev"]},
            {"id": "debugging", "name": "Debugging",
             "description": "问题定位与修复", "tags": ["dev"]},
            {"id": "refactor", "name": "Refactor",
             "description": "重构", "tags": ["dev"]},
            {"id": "git", "name": "Git",
             "description": "版本控制操作", "tags": ["dev"]},
            {"id": "code_review", "name": "Code Review",
             "description": "代码评审", "tags": ["review"]},
        ],
    }
