"""Configuration and .env loading."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from notebook_alchemy.config import (
    DEFAULT_GEMINI_MODEL,
    AlchemyConfig,
    _load_dotenv_simple,
    is_usable_api_key,
)
from notebook_alchemy.errors import ConfigError


def test_defaults() -> None:
    config = AlchemyConfig()
    assert config.env == "development"
    assert config.vault_path.name == "vault"
    assert config.gemini_model == DEFAULT_GEMINI_MODEL
    assert config.max_concurrency == 4
    assert config.session_file is None


@pytest.mark.parametrize(
    ("key", "usable"),
    [("", False), ("your_gemini_api_key_here", False), ("short", False), ("A" * 20, True)],
)
def test_api_key_validity(key: str, usable: bool) -> None:
    assert is_usable_api_key(key) is usable


def test_dotenv_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    Path(".env").write_text(
        "# comment\n"
        "export GEMINI_API_KEY='" + "K" * 20 + "'\n"
        'ALCHEMY_ENV="staging"\n'
        "ALCHEMY_LOG_LEVEL=debug # inline comment\n"
        "ALREADY_SET=from-file\n"
        "not a pair\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ALREADY_SET", "from-process")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _load_dotenv_simple()
    assert os.environ["GEMINI_API_KEY"] == "K" * 20
    assert os.environ["ALCHEMY_ENV"] == "staging"
    assert os.environ["ALCHEMY_LOG_LEVEL"] == "debug"
    assert os.environ["ALREADY_SET"] == "from-process"  # a real variable is never overridden
    monkeypatch.delenv("ALCHEMY_ENV")
    monkeypatch.delenv("ALCHEMY_LOG_LEVEL")


def test_from_env_reads_dotenv_key() -> None:
    Path(".env").write_text("GEMINI_API_KEY=" + "Z" * 24 + "\n", encoding="utf-8")
    try:
        assert AlchemyConfig.from_env().has_gemini_key
    finally:
        os.environ.pop("GEMINI_API_KEY", None)


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
    monkeypatch.setenv("ALCHEMY_GEMINI_MODEL", "gemini-test-model")
    assert AlchemyConfig.from_env().gemini_model == "gemini-test-model"
