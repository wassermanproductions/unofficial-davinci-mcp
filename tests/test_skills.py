"""Tests for the editorial-knowledge skills package.

Verifies: every topic loads, the index is complete, aliases resolve, the tool
handler returns well-formed JSON, and every repo tool name referenced in the
markdown actually exists in ARCHITECTURE.md's tool surface.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import skills

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SKILLS_DIR = _REPO_ROOT / "skills"
_ARCHITECTURE = _REPO_ROOT / "ARCHITECTURE.md"

_EXPECTED_TOPICS = {
    "color-looks",
    "beat-cutting",
    "dialogue-editing",
    "music-editing",
    "mixing",
}


def _registered_tool_surface() -> set[str]:
    """Tool names actually registered by the server - the live source of truth."""
    from davinci_mcp.registry import build_registry

    registry = build_registry()
    return {tool["name"] for tool in registry.list_mcp()}


def test_index_is_complete_and_ordered():
    index = skills.list_topics()
    slugs = [entry["topic"] for entry in index]
    assert set(slugs) == _EXPECTED_TOPICS
    assert len(slugs) == len(set(slugs)) == 5
    for entry in index:
        assert entry["summary"].strip()


@pytest.mark.parametrize("topic", sorted(_EXPECTED_TOPICS))
def test_every_topic_loads(topic):
    result = skills.get_editing_knowledge(topic)
    assert result["ok"] is True
    assert result["topic"] == topic
    assert len(result["text"]) > 2000  # substantive, not a stub
    # The index travels with every response for onward navigation.
    assert {e["topic"] for e in result["topics"]} == _EXPECTED_TOPICS


@pytest.mark.parametrize("topic", sorted(_EXPECTED_TOPICS))
def test_each_file_within_line_bounds(topic):
    filename = f"{topic}.md"
    lines = (_SKILLS_DIR / filename).read_text(encoding="utf-8").splitlines()
    assert 150 <= len(lines) <= 300, f"{filename} has {len(lines)} lines"


def test_no_topic_returns_index_only():
    result = skills.get_editing_knowledge()
    assert result["ok"] is True
    assert result["topic"] is None
    assert "text" not in result
    assert {e["topic"] for e in result["topics"]} == _EXPECTED_TOPICS
    # "index" / "topics" behave the same.
    for word in ("index", "topics", "", "list"):
        assert skills.get_editing_knowledge(word)["topic"] is None


def test_aliases_resolve():
    assert skills.get_editing_knowledge("color")["topic"] == "color-looks"
    assert skills.get_editing_knowledge("LUTs")["topic"] == "color-looks"
    assert skills.get_editing_knowledge("beat")["topic"] == "beat-cutting"
    assert skills.get_editing_knowledge("dialog")["topic"] == "dialogue-editing"
    assert skills.get_editing_knowledge("song")["topic"] == "music-editing"
    assert skills.get_editing_knowledge("loudness")["topic"] == "mixing"
    assert skills.get_editing_knowledge("mixing.md")["topic"] == "mixing"


def test_unknown_topic_is_soft_error_with_index():
    result = skills.get_editing_knowledge("does-not-exist")
    assert result["ok"] is False
    assert "error" in result
    assert {e["topic"] for e in result["topics"]} == _EXPECTED_TOPICS


def test_read_topic_raises_on_unknown():
    with pytest.raises(KeyError):
        skills.read_topic("nope")


def test_tool_handler_returns_valid_json():
    payload = skills._handle_get_editing_knowledge({"topic": "mixing"})
    parsed = json.loads(payload)
    assert parsed["ok"] is True
    assert parsed["topic"] == "mixing"
    assert "-16" in parsed["text"]  # the LUFS anchor is in there
    # No-arg call is safe too.
    empty = json.loads(skills._handle_get_editing_knowledge(None))
    assert empty["topic"] is None


def test_register_uses_convention():
    calls = []

    def add_tool(name, schema, handler, tier="both", description=""):
        calls.append((name, schema, handler, tier, description))

    skills.register(add_tool)
    assert len(calls) == 1
    name, schema, handler, tier, description = calls[0]
    assert name == "get_editing_knowledge"
    assert tier == "both"
    assert description
    assert schema["type"] == "object"
    assert set(schema["properties"]["topic"]["enum"]) == _EXPECTED_TOPICS
    # The registered handler round-trips.
    out = json.loads(handler({"topic": "color-looks"}))
    assert out["topic"] == "color-looks"


def test_register_tolerates_positional_signature():
    calls = []

    # A registry whose parameter isn't named 'tier' rejects the keyword call in
    # register() and forces the positional fallback path.
    def strict_add_tool(name, schema, handler, mode, description):
        calls.append((name, mode, description))

    skills.register(strict_add_tool)
    assert calls and calls[0][0] == "get_editing_knowledge"
    assert calls[0][1] == "both"


def test_referenced_tool_names_exist_in_registry():
    surface = _registered_tool_surface()
    # Sanity: the parse actually found the surface.
    assert "get_editing_knowledge" in surface
    assert "assemble_edit" in surface

    # Any identifier written as `name(` in a skill is a tool reference; it must
    # exist in the architecture's tool surface.
    referenced: set[str] = set()
    for md in _SKILLS_DIR.glob("*.md"):
        text = md.read_text(encoding="utf-8")
        referenced |= set(re.findall(r"\b([a-z_][a-z0-9_]+)\(", text))

    missing = referenced - surface
    assert not missing, f"skills reference tools the server does not register: {sorted(missing)}"
