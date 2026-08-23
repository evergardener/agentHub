"""生产镜像供应链配置的静态回归门禁。"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SHA256 = r"sha256:[0-9a-f]{64}"
ACTION_SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def test_project_branding_and_compose_identity_are_explicit():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())

    assert project["project"]["name"] == "agenthub"
    assert compose["name"] == "agenthub"
    assert compose["networks"]["default"]["name"] == "agenthub_default"


def test_all_external_runtime_images_are_digest_pinned():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    external = ("nats", "postgres", "jaeger")
    for service in external:
        image = compose["services"][service]["image"]
        assert re.search(rf":[^@\s]+@{SHA256}$", image), image

    dockerfile = (ROOT / "Dockerfile").read_text()
    assert dockerfile.count("@${PYTHON_DIGEST}") == 2
    assert re.search(rf"ARG PYTHON_DIGEST={SHA256}$", dockerfile, re.MULTILINE)


def test_all_published_control_plane_ports_bind_loopback():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for service_name, service in compose["services"].items():
        for published in service.get("ports", []):
            assert str(published).startswith("127.0.0.1:"), (
                service_name, published)


def test_agentctl_compose_service_preserves_cli_entrypoint_for_subcommands():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["agentctl"]
    assert service["entrypoint"] == ["agentctl"]
    assert service["command"] == ["chat"]


def test_janitor_can_read_host_adapter_artifacts():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    janitor = compose["services"]["janitor"]
    mounts = janitor["volumes"]
    assert "${HOME}/AgentWorkspace:${HOME}/AgentWorkspace:ro" in mounts
    assert janitor["environment"]["LAS_ARTIFACT_ROOTS"] == (
        "/data/workspace,${HOME}/AgentWorkspace")


def test_release_smoke_targets_codex_and_dsh_without_kimi():
    smoke = (ROOT / "scripts" / "e2e_phase_a.py").read_text()
    assert '"codex"' in smoke
    assert '"dsh"' in smoke
    assert 'TOKENS["kimi"]' not in smoke
    assert '"tasks/reject"' in smoke


def test_agentgateway_download_is_verified_per_supported_architecture():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "agentgateway/releases/download/v1.4.1" in dockerfile
    assert re.search(r'amd64\) expected="[0-9a-f]{64}"', dockerfile)
    assert re.search(r'arm64\) expected="[0-9a-f]{64}"', dockerfile)
    assert 'sha256sum -c -' in dockerfile
    assert "unsupported agentgateway architecture" in dockerfile


def test_container_dependencies_are_exactly_locked_and_cached_before_source():
    locked_lines = [
        line.strip() for line in (ROOT / "requirements.lock").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert locked_lines
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^=\s]+", line)
               for line in locked_lines)
    locked = {line.split("==", 1)[0].lower().replace("_", "-")
              for line in locked_lines}
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    direct = {
        re.split(r"[<>=\[]", requirement, 1)[0].lower().replace("_", "-")
        for requirement in project["project"]["dependencies"]
    }
    assert direct <= locked
    assert project["project"]["name"] not in locked

    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "-r requirements.lock" in dockerfile
    assert dockerfile.index("COPY requirements.lock") < dockerfile.index(
        "COPY src ./src")
    assert "pip install --no-cache-dir" in dockerfile


def test_release_workflow_pins_actions_and_enforces_attest_scan_sign():
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "docker.yml").read_text()
    )
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
    ]
    action_refs = [step["uses"] for step in steps if "uses" in step]
    assert action_refs
    assert all(ACTION_SHA.fullmatch(ref) for ref in action_refs), action_refs

    build = next(step for step in steps if step.get("id") == "build")
    assert build["with"]["provenance"] == "mode=max"
    assert build["with"]["sbom"] is True
    assert "candidate-${{ github.sha }}" in build["with"]["tags"]
    assert "steps.meta.outputs.tags" not in build["with"]["tags"]

    trivy = next(step for step in steps if step.get("uses", "").startswith(
        "aquasecurity/trivy-action@"
    ))
    assert trivy["with"]["exit-code"] == "1"
    assert trivy["with"]["severity"] == "HIGH,CRITICAL"
    assert "steps.build.outputs.digest" in trivy["with"]["image-ref"]

    sign = next(step for step in steps if "cosign sign" in step.get("run", ""))
    assert "${IMAGE}@${DIGEST}" in sign["run"]
    promote = next(
        step for step in steps if "imagetools create" in step.get("run", "")
    )
    assert promote["env"]["TAGS"] == "${{ steps.meta.outputs.tags }}"
    assert steps.index(trivy) < steps.index(sign) < steps.index(promote)
    assert workflow["jobs"]["build"]["permissions"]["id-token"] == "write"
