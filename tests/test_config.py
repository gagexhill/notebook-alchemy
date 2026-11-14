"""Configuration and .env loading."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from notebook_alchemy.config import (
    DEFAULT_MODEL,
    AlchemyConfig,
    _load_dotenv_simple,
    is_usable_api_key,
    update_dotenv,
    validate_profile_name,
)
from notebook_alchemy.errors import ConfigError


def test_defaults() -> None:
    config = AlchemyConfig()
    assert config.env == "development"
    assert config.cache_dir.name == "cache" and config.cookies_browser is None
    assert config.model == DEFAULT_MODEL
    assert config.max_concurrency == 4
    assert config.session_file is None


@pytest.mark.parametrize(
    ("key", "usable"),
    [("", False), ("your_anthropic_api_key_here", False), ("short", False), ("A" * 20, True)],
)
def test_api_key_validity(key: str, usable: bool) -> None:
    assert is_usable_api_key(key) is usable


def test_dotenv_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    Path(".env").write_text(
        "# comment\n"
        "export ANTHROPIC_API_KEY='" + "K" * 20 + "'\n"
        'ALCHEMY_ENV="staging"\n'
        "ALCHEMY_LOG_LEVEL=debug # inline comment\n"
        "ALREADY_SET=from-file\n"
        "not a pair\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ALREADY_SET", "from-process")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _load_dotenv_simple()
    assert os.environ["ANTHROPIC_API_KEY"] == "K" * 20
    assert os.environ["ALCHEMY_ENV"] == "staging"
    assert os.environ["ALCHEMY_LOG_LEVEL"] == "debug"
    assert os.environ["ALREADY_SET"] == "from-process"  # a real variable is never overridden
    monkeypatch.delenv("ALCHEMY_ENV")
    monkeypatch.delenv("ALCHEMY_LOG_LEVEL")


def test_from_env_reads_dotenv_key() -> None:
    Path(".env").write_text("ANTHROPIC_API_KEY=" + "Z" * 24 + "\n", encoding="utf-8")
    try:
        assert AlchemyConfig.from_env().has_api_key
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


@pytest.mark.parametrize("value", ["abc", "0", "99"])
def test_invalid_concurrency_is_a_config_error(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("ALCHEMY_MAX_CONCURRENCY", value)
    with pytest.raises(ConfigError) as err:
        AlchemyConfig.from_env()
    assert "ALCHEMY_MAX_CONCURRENCY" in err.value.message
    assert err.value.hint


def test_invalid_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALCHEMY_LOG_LEVEL", "loud")
    with pytest.raises(ConfigError):
        AlchemyConfig.from_env()


def test_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALCHEMY_MODEL", "claude-test-model")
    assert AlchemyConfig.from_env().model == "claude-test-model"


def test_notebooklm_profile_default_override_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    assert AlchemyConfig.from_env().notebooklm_profile == "default"
    monkeypatch.setenv("ALCHEMY_NOTEBOOKLM_PROFILE", "personal")
    assert AlchemyConfig.from_env().notebooklm_profile == "personal"
    monkeypatch.setenv("ALCHEMY_NOTEBOOKLM_PROFILE", "../escape")
    with pytest.raises(ConfigError, match="profile name"):
        AlchemyConfig.from_env()


def test_update_dotenv_replaces_in_place_keeps_other_lines_and_round_trips() -> None:
    env = Path(".env")
    env.write_text(
        "# my settings\nANTHROPIC_API_KEY=old_value_123456789\nOTHER=keep\nANTHROPIC_API_KEY=dup\n",
        encoding="utf-8",
    )
    update_dotenv({"ANTHROPIC_API_KEY": "new_value_123456789", "ALCHEMY_VAULT_PATH": "C:/My Vault"})
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "# my settings",
        "ANTHROPIC_API_KEY=new_value_123456789",
        "OTHER=keep",
        'ALCHEMY_VAULT_PATH="C:/My Vault"',
    ]
    _load_dotenv_simple(env)
    assert os.environ["ANTHROPIC_API_KEY"] == "new_value_123456789"
    assert os.environ["ALCHEMY_VAULT_PATH"] == "C:/My Vault"


@pytest.mark.parametrize("value", ['has"quote', "two\nlines", " padded"])
def test_update_dotenv_rejects_values_it_cannot_round_trip(value: str) -> None:
    with pytest.raises(ConfigError):
        update_dotenv({"ANTHROPIC_API_KEY": value})
    assert not Path(".env").exists()


@pytest.mark.parametrize("name", ["nul", "COM1", "work.", "a.b", "-x", "", "x" * 65])
def test_unusable_profile_names_are_refused(name: str) -> None:
    with pytest.raises(ConfigError, match="profile name"):
        validate_profile_name(name)


def test_update_dotenv_replaces_export_lines_and_quotes_hash_values() -> None:
    env = Path(".env")
    env.write_text("export ANTHROPIC_API_KEY=old_value_123456789\n", encoding="utf-8")
    update_dotenv({"ANTHROPIC_API_KEY": "new_value_123456789", "ALCHEMY_VAULT_PATH": "C:/a#b"})
    assert env.read_text(encoding="utf-8").splitlines() == [
        "ANTHROPIC_API_KEY=new_value_123456789",
        'ALCHEMY_VAULT_PATH="C:/a#b"',
    ]
    _load_dotenv_simple(env)
    assert os.environ["ALCHEMY_VAULT_PATH"] == "C:/a#b"


def test_a_byte_order_mark_does_not_hide_the_first_key(monkeypatch: pytest.MonkeyPatch) -> None:
    env = Path(".env")
    env.write_bytes("\ufeffANTHROPIC_API_KEY=old_value_123456789\nOTHER=1\n".encode())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _load_dotenv_simple(env)
    assert os.environ["ANTHROPIC_API_KEY"] == "old_value_123456789"
    update_dotenv({"ANTHROPIC_API_KEY": "new_value_123456789"}, env)
    text = env.read_text(encoding="utf-8-sig")
    assert text.count("ANTHROPIC_API_KEY=") == 1 and "old_value" not in text and "OTHER=1" in text


def test_caption_languages_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALCHEMY_CAPTION_LANGUAGES", " de, en ,,")
    assert AlchemyConfig.from_env().caption_languages == ("de", "en")


def test_a_dotenv_sets_only_this_tools_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("ANTHROPIC_BASE_URL", "NOTEBOOKLM_HOME", "ALCHEMY_MODEL"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    lines = [
        "ANTHROPIC_BASE_URL=https://collector.example.test",
        "NOTEBOOKLM_HOME=/elsewhere",
        "ALCHEMY_MODEL=claude-x",
    ]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    from notebook_alchemy.config import _load_dotenv_simple

    _load_dotenv_simple(env)
    assert os.environ.get("ALCHEMY_MODEL") == "claude-x"
    assert "ANTHROPIC_BASE_URL" not in os.environ and "NOTEBOOKLM_HOME" not in os.environ
    monkeypatch.delenv("ALCHEMY_MODEL")
