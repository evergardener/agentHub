"""Agent Card for the DeepSeek Harness worker."""

from __future__ import annotations


def agent_card(base_url: str) -> dict:
    return {
        "name": "dsh",
        "description": (
            "DeepSeek Harness Worker: persistent coding sessions, analysis, "
            "tool execution, sub-agent collaboration, and independent review."
        ),
        "url": base_url,
        "version": "0.1.0",
        "protocolVersion": "0.3",
        "capabilities": {
            # Standard A2A message/stream is not exposed yet. Native/polled
            # session events are declared separately in agentHubSession.
            "streaming": False,
            "pushNotifications": False,
            "extensions": {
                "agentHubSecurity": {
                    "nativePermissionEnforcement": True,
                    "permissionPreset": "read-only",
                    "approvalPolicy": "ask",
                    "modelPromptsDefault": "after-native-verification",
                    "modifyingOperations": "approval-required",
                },
            },
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text", "file"],
        "skills": [
            {
                "id": "coding",
                "name": "Coding",
                "description": "Implement, debug, and refactor code",
                "tags": ["dev"],
            },
            {
                "id": "code_review",
                "name": "Code Review",
                "description": "Review implementation, tests, and risks",
                "tags": ["review"],
            },
            {
                "id": "analysis",
                "name": "Analysis",
                "description": "Long-running technical analysis",
                "tags": ["analysis"],
            },
            {
                "id": "agent_collaboration",
                "name": "Agent Collaboration",
                "description": "Coordinate DSH-native sub-agents and workflows",
                "tags": ["collaboration"],
            },
        ],
    }
