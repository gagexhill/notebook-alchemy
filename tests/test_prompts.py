"""The AI Studio blueprint mirrors the code; this fails when the copies disagree."""

from __future__ import annotations

import json
from pathlib import Path

from notebook_alchemy.config import DEFAULT_GEMINI_MODEL
from notebook_alchemy.knowledge import LABEL_MAX_OUTPUT_TOKENS, SYSTEM, request_body

ROOT = Path(__file__).resolve().parent.parent


def test_agent_config_matches_the_code() -> None:
    config = json.loads((ROOT / "prompts" / "ai_studio_agent_config.json").read_text(encoding="utf-8"))
    assert config["model"] == DEFAULT_GEMINI_MODEL
    generation = config["generationConfig"]
    assert generation["maxOutputTokens"] == LABEL_MAX_OUTPUT_TOKENS
    assert generation["responseMimeType"] == request_body("x")["generationConfig"]["responseMimeType"]
    if DEFAULT_GEMINI_MODEL.startswith("gemini-3"):
        assert generation.get("thinkingConfig") == {"thinkingLevel": "low"}
        assert "temperature" not in generation  # not offered for Gemini 3.x
    assert config["systemInstruction"]["parts"][0]["text"] == SYSTEM


def test_blueprint_document_quotes_the_system_instruction() -> None:
    doc = (ROOT / "prompts" / "ai_studio_system_instruction.md").read_text(encoding="utf-8")
    assert SYSTEM in doc and DEFAULT_GEMINI_MODEL in doc
