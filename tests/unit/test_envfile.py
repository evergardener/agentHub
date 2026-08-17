"""common.envfile.ensure_key — .env 配置项自动初始化（v3 加固）。"""

from __future__ import annotations

from common.envfile import ensure_key


def test_generates_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_CFG_KEY", raising=False)
    env = tmp_path / ".env"
    value, created = ensure_key(env, "TEST_CFG_KEY")
    assert created and value
    assert f"TEST_CFG_KEY={value}" in env.read_text()


def test_keeps_existing_value(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_CFG_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("TEST_CFG_KEY=pre-set\n")
    value, created = ensure_key(env, "TEST_CFG_KEY")
    assert (value, created) == ("pre-set", False)
    assert env.read_text() == "pre-set\n" or "pre-set" in env.read_text()


def test_idempotent_second_call(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_CFG_KEY", raising=False)
    env = tmp_path / ".env"
    v1, c1 = ensure_key(env, "TEST_CFG_KEY")
    v2, c2 = ensure_key(env, "TEST_CFG_KEY")
    assert c1 and not c2 and v1 == v2


def test_process_env_takes_precedence_without_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_CFG_KEY", "from-env")
    env = tmp_path / ".env"
    value, created = ensure_key(env, "TEST_CFG_KEY")
    assert (value, created) == ("from-env", False)
    assert not env.exists() or "TEST_CFG_KEY" not in env.read_text()


def test_appends_without_trailing_newline(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_CFG_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("OTHER=1")  # 无结尾换行
    value, _ = ensure_key(env, "TEST_CFG_KEY", generator=lambda: "gen-val")
    text = env.read_text()
    assert "OTHER=1\nTEST_CFG_KEY=gen-val\n" in text


def test_custom_generator(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_CFG_KEY", raising=False)
    value, created = ensure_key(tmp_path / ".env", "TEST_CFG_KEY",
                                generator=lambda: "fixed")
    assert (value, created) == ("fixed", True)
