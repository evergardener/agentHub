"""Static safety contracts for macOS deployment launchers."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_dsh_web_launchagent_is_loopback_only_and_persistent():
    script = (ROOT / "scripts" / "install-dsh-web-autostart.sh").read_text()

    assert "<string>127.0.0.1</string>" in script
    assert "<string>3080</string>" in script
    assert "<key>RunAtLoad</key>" in script
    assert "<key>KeepAlive</key>" in script
    assert "0.0.0.0" not in script
    assert "plutil -lint" in script


def test_worker_launcher_has_dedicated_dsh_adapter_port():
    script = (ROOT / "scripts" / "agent-worker.sh").read_text()
    assert "dsh)   DEFAULT_PORT=8203" in script
