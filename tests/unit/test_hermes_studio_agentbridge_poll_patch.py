from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / \
    "patch-hermes-studio-agentbridge-poll.py"


def _load_patch_module():
    spec = importlib.util.spec_from_file_location(
        "hermes_studio_agentbridge_poll_patch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _fake_package(tmp_path: Path, module) -> tuple[Path, bytes]:
    root = tmp_path / "hermes-web-ui"
    runtime = root / "dist" / "server" / "index.js"
    runtime.parent.mkdir(parents=True)
    original = (
        b"prefix;" + module.ORIGINAL_TIMER + b";middle;" +
        module.ORIGINAL_GATE + b";schedule;" +
        module.ORIGINAL_SCHEDULE + b";propagation;" +
        module.ORIGINAL_PROPAGATION + b";handle-signature;" +
        module.ORIGINAL_HANDLE_RUN_SIGNATURE + b";bridge-signature;" +
        module.ORIGINAL_BRIDGE_RUN_SIGNATURE + b";bridge-call;" +
        module.ORIGINAL_BRIDGE_RUN_CALL + b";guard;" +
        module.ORIGINAL_CONTEXT_GUARD + b";failure;" +
        module.ORIGINAL_CONTEXT_FAILURE + b"suffix\n"
    )
    runtime.write_bytes(original)
    (root / "package.json").write_text(
        json.dumps({"name": "hermes-web-ui", "version": "0.6.47"}),
        encoding="utf-8",
    )
    return root, original


def test_patch_makes_idle_bridge_poll_and_is_idempotent(tmp_path):
    module = _load_patch_module()
    root, original = _fake_package(tmp_path, module)
    backup_root = tmp_path / "backups"
    expected = hashlib.sha256(original).hexdigest()

    applied = module.apply_package_patch(
        root, backup_root=backup_root, expected_original_sha256=expected)

    assert applied["status"] == "applied"
    runtime = (root / "dist" / "server" / "index.js").read_bytes()
    assert module.PATCHED_TIMER in runtime
    assert module.PATCHED_GATE in runtime
    assert module.PATCHED_SCHEDULE in runtime
    assert module.PATCHED_PROPAGATION in runtime
    assert module.PATCHED_HANDLE_RUN_SIGNATURE in runtime
    assert module.PATCHED_BRIDGE_RUN_SIGNATURE in runtime
    assert module.PATCHED_BRIDGE_RUN_CALL in runtime
    assert module.PATCHED_CONTEXT_GUARD in runtime
    assert module.PATCHED_CONTEXT_FAILURE in runtime
    assert module.ORIGINAL_TIMER not in runtime
    assert module.ORIGINAL_GATE not in runtime
    assert module.ORIGINAL_SCHEDULE not in runtime
    assert module.ORIGINAL_PROPAGATION not in runtime
    assert module.ORIGINAL_HANDLE_RUN_SIGNATURE not in runtime
    assert module.ORIGINAL_BRIDGE_RUN_SIGNATURE not in runtime
    assert module.ORIGINAL_BRIDGE_RUN_CALL not in runtime
    assert module.ORIGINAL_CONTEXT_GUARD not in runtime
    assert module.ORIGINAL_CONTEXT_FAILURE not in runtime
    assert Path(applied["backup_path"]).read_bytes() == original

    again = module.apply_package_patch(
        root, backup_root=backup_root, expected_original_sha256=expected)
    assert again["status"] == "already_applied"


def test_patch_restore_round_trip_is_exact(tmp_path):
    module = _load_patch_module()
    root, original = _fake_package(tmp_path, module)
    expected = hashlib.sha256(original).hexdigest()
    applied = module.apply_package_patch(
        root, backup_root=tmp_path / "backups",
        expected_original_sha256=expected)

    restored = module.restore_package_patch(
        root, Path(applied["backup_path"]))

    assert restored["status"] == "restored"
    assert (root / "dist" / "server" / "index.js").read_bytes() == original


def test_patch_upgrades_the_pinned_poll_only_runtime(tmp_path, monkeypatch):
    module = _load_patch_module()
    root, original = _fake_package(tmp_path, module)
    poll_only = original.replace(
        module.ORIGINAL_TIMER, module.PATCHED_TIMER).replace(
        module.ORIGINAL_GATE, module.PATCHED_GATE)
    runtime_path = root / "dist" / "server" / "index.js"
    runtime_path.write_bytes(poll_only)
    monkeypatch.setattr(
        module, "POLL_ONLY_RUNTIME_SHA256",
        hashlib.sha256(poll_only).hexdigest())

    applied = module.apply_package_patch(
        root,
        backup_root=tmp_path / "backups",
        expected_original_sha256=hashlib.sha256(original).hexdigest(),
    )

    assert applied["status"] == "applied"
    assert Path(applied["backup_path"]).read_bytes() == poll_only
    assert module.inspect_package(root)["state"] == "patched"


def test_claimed_recovery_marker_is_passed_out_of_band(tmp_path):
    module = _load_patch_module()
    root, original = _fake_package(tmp_path, module)
    expected = hashlib.sha256(original).hexdigest()

    module.apply_package_patch(
        root, backup_root=tmp_path / "backups",
        expected_original_sha256=expected)
    runtime = (root / "dist" / "server" / "index.js").read_bytes()

    assert b"trustedBackgroundRecovery:!0" in runtime
    assert b"trusted_background_recovery" not in runtime
    assert b"a.trustedBackgroundRecovery===!0" in runtime
    assert b",c,o,u=!1){" in runtime
    assert b"I.background_delegation_id&&!N&&!u" in runtime


def test_missing_untrusted_context_releases_instead_of_completes(tmp_path):
    module = _load_patch_module()
    root, original = _fake_package(tmp_path, module)

    module.apply_package_patch(
        root, backup_root=tmp_path / "backups",
        expected_original_sha256=hashlib.sha256(original).hexdigest())
    runtime = (root / "dist" / "server" / "index.js").read_bytes()

    assert module.PATCHED_CONTEXT_FAILURE in runtime
    assert module.ORIGINAL_CONTEXT_FAILURE not in runtime


def test_patch_fails_closed_on_unknown_runtime(tmp_path):
    module = _load_patch_module()
    root, original = _fake_package(tmp_path, module)

    with pytest.raises(module.PatchError, match="SHA-256"):
        module.apply_package_patch(
            root, backup_root=tmp_path / "backups",
            expected_original_sha256="0" * 64)

    assert (root / "dist" / "server" / "index.js").read_bytes() == original


def test_patch_fails_closed_on_unsupported_version(tmp_path):
    module = _load_patch_module()
    root, original = _fake_package(tmp_path, module)
    (root / "package.json").write_text(
        json.dumps({"name": "hermes-web-ui", "version": "0.6.48"}),
        encoding="utf-8",
    )

    with pytest.raises(module.PatchError, match="unsupported"):
        module.apply_package_patch(
            root, backup_root=tmp_path / "backups",
            expected_original_sha256=hashlib.sha256(original).hexdigest())
