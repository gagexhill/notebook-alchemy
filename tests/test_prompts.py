"""The AI Studio blueprint mirrors the code; this fails when the copies disagree."""

from __future__ import annotations

import json
from pathlib import Path

from notebook_alchemy.config import DEFAULT_GEMINI_MODEL
from notebook_alchemy.synthesizer import LIVE_MAX_OUTPUT_TOKENS, SYSTEM_PROMPT

ROOT = Path(__file__).resolve().parent.parent


def test_agent_config_matches_the_code() -> None:
    config = json.loads((ROOT / "prompts" / "ai_studio_agent_config.json").read_text(encoding="utf-8"))
    assert config["model"] == DEFAULT_GEMINI_MODEL
    assert config["generationConfig"]["maxOutputTokens"] == LIVE_MAX_OUTPUT_TOKENS
    thinking = config["generationConfig"].get("thinkingConfig")
    if DEFAULT_GEMINI_MODEL.startswith("gemini-3"):
        assert thinking == {"thinkingLevel": "low"}
        assert "temperature" not in config["generationConfig"]  # not offered for Gemini 3.x
    elif DEFAULT_GEMINI_MODEL.startswith("gemini-2.5"):
        assert thinking == {"thinkingBudget": 0}
    assert config["systemInstruction"]["parts"][0]["text"] == SYSTEM_PROMPT


def test_blueprint_document_quotes_the_system_instruction() -> None:
    doc = (ROOT / "prompts" / "ai_studio_system_instruction.md").read_text(encoding="utf-8")
    assert SYSTEM_PROMPT in doc
    assert DEFAULT_GEMINI_MODEL in doc
