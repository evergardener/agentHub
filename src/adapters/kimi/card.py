"""Kimi Adapter 的 Agent Card。"""

from __future__ import annotations


def agent_card(base_url: str) -> dict:
    return {
        "name": "kimi",
        "description": "Kimi Worker（真实本地 Kimi Code CLI）：调研、长上下文"
                       "分析、文档理解、中文内容、摘要、代码与文件任务。",
        "url": base_url,
        "version": "0.2.0",
        "protocolVersion": "0.3",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text", "file"],
        "skills": [
            {"id": "research", "name": "Research",
             "description": "调研与分析", "tags": ["research"]},
            {"id": "long_context", "name": "Long Context",
             "description": "长上下文处理", "tags": ["research"]},
            {"id": "document_analysis", "name": "Document Analysis",
             "description": "文档分析", "tags": ["research"]},
            {"id": "summarization", "name": "Summarization",
             "description": "摘要", "tags": ["research"]},
        ],
    }
