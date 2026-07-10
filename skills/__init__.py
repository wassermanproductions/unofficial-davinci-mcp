"""Editorial knowledge served to any MCP client.

Five markdown files written like a veteran editor training a sharp assistant:
concrete numbers, decision rules, and explicit mappings to this repo's tool
parameters. Exposed to agents through the ``get_editing_knowledge`` tool so every
client benefits, not just ones that can read the repo.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

_SKILLS_DIR = Path(__file__).resolve().parent

# topic slug -> (filename, one-line summary for the index)
_TOPICS: dict[str, tuple[str, str]] = {
    "color-looks": (
        "color-looks.md",
        "Look vocabulary (teal-orange, bleach bypass, day-for-night, noir, "
        "Kodak/Fuji...) mapped to color_match / apply_lut moves; reading a "
        "reference image; LUT etiquette.",
    ),
    "beat-cutting": (
        "beat-cutting.md",
        "Cutting picture to music: energy-to-density mapping, cut-on-beat vs "
        "1-2 frames early, beat_grid + assemble_edit beat-snap tolerances, when "
        "not to snap.",
    ),
    "dialogue-editing": (
        "dialogue-editing.md",
        "Dead-air removal that still breathes: max_pause by content type, "
        "keep-handles, breath handling, hiding jump cuts, mapping to "
        "tighten_dialogue.",
    ),
    "music-editing": (
        "music-editing.md",
        "Cutting a song to length on phrase boundaries: finding the exit, "
        "ring-out vs button endings, fade lengths, mapping to cut_music / "
        "mix_plan.",
    ),
    "mixing": (
        "mixing.md",
        "The rough-mix recipe: dialogue LUFS anchors, music-bed levels, ducking "
        "depth/ramps, true-peak/LRA checks, mapping to mix_plan; when a human is "
        "needed.",
    ),
}

# Convenience aliases so agents can ask naturally.
_ALIASES: dict[str, str] = {
    "color": "color-looks",
    "colors": "color-looks",
    "color_looks": "color-looks",
    "looks": "color-looks",
    "grading": "color-looks",
    "grade": "color-looks",
    "lut": "color-looks",
    "luts": "color-looks",
    "beat": "beat-cutting",
    "beats": "beat-cutting",
    "beat_cutting": "beat-cutting",
    "cutting": "beat-cutting",
    "montage": "beat-cutting",
    "dialogue": "dialogue-editing",
    "dialog": "dialogue-editing",
    "dialogue_editing": "dialogue-editing",
    "dead_air": "dialogue-editing",
    "tighten": "dialogue-editing",
    "music": "music-editing",
    "music_editing": "music-editing",
    "song": "music-editing",
    "mix": "mixing",
    "mixing_": "mixing",
    "loudness": "mixing",
    "audio": "mixing",
}


def list_topics() -> list[dict[str, str]]:
    """Return the topics index: slug + one-line summary, in canonical order."""
    return [
        {"topic": slug, "summary": summary}
        for slug, (_filename, summary) in _TOPICS.items()
    ]


def _canonical(topic: str) -> str | None:
    key = (topic or "").strip().lower().replace(" ", "-")
    if key in _TOPICS:
        return key
    underscored = key.replace("-", "_")
    if underscored in _ALIASES:
        return _ALIASES[underscored]
    if key in _ALIASES:
        return _ALIASES[key]
    # tolerate a trailing ".md"
    if key.endswith(".md"):
        stem = key[:-3]
        if stem in _TOPICS:
            return stem
    return None


def read_topic(topic: str) -> str:
    """Return the raw markdown for a topic. Raises KeyError if unknown."""
    slug = _canonical(topic)
    if slug is None:
        raise KeyError(topic)
    filename, _summary = _TOPICS[slug]
    return (_SKILLS_DIR / filename).read_text(encoding="utf-8")


def get_editing_knowledge(topic: str | None = None) -> dict[str, Any]:
    """Serve editorial knowledge to an agent.

    With no ``topic`` (or ``"index"`` / ``"topics"``), returns just the index so
    the agent can pick. With a valid topic (or alias), returns the full markdown
    text plus the index for onward navigation. Unknown topics return an error and
    the index rather than raising, so a tool call always yields something useful.
    """
    index = list_topics()

    if topic is None or str(topic).strip().lower() in {"", "index", "topics", "list"}:
        return {
            "ok": True,
            "topic": None,
            "topics": index,
            "hint": "Call get_editing_knowledge with a topic slug for the full text.",
        }

    slug = _canonical(topic)
    if slug is None:
        return {
            "ok": False,
            "error": f"Unknown topic {topic!r}.",
            "topics": index,
        }

    return {
        "ok": True,
        "topic": slug,
        "summary": _TOPICS[slug][1],
        "text": read_topic(slug),
        "topics": index,
    }


# --- MCP tool registration --------------------------------------------------

GET_EDITING_KNOWLEDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {
            "type": "string",
            "description": (
                "Editorial topic to load. One of: "
                + ", ".join(_TOPICS)
                + ". Omit (or use 'index') to list topics."
            ),
            "enum": list(_TOPICS.keys()),
        }
    },
    "required": [],
    "additionalProperties": False,
}

_DESCRIPTION = (
    "Read editorial knowledge for editing well (not just operating the app): "
    "color looks, beat-cutting, dialogue editing, music editing, and mixing — "
    "with concrete numbers and mappings to this server's tool parameters. Omit "
    "topic to list what's available."
)


def _handle_get_editing_knowledge(params: dict[str, Any] | None, **_kwargs: Any) -> str:
    params = params or {}
    topic = params.get("topic")
    try:
        result = get_editing_knowledge(topic)
    except Exception as exc:  # pragma: no cover - defensive
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return json.dumps(result, sort_keys=True)


def register(add_tool: Callable[..., Any]) -> None:
    """Register the editorial-knowledge tool with the server registry.

    Follows the repo registry convention:
    ``add_tool(name, schema, handler, tier='both', description)``. Kept resilient
    to keyword vs positional calling so it works regardless of the exact
    signature the core registry lands on.
    """
    try:
        add_tool(
            "get_editing_knowledge",
            GET_EDITING_KNOWLEDGE_SCHEMA,
            _handle_get_editing_knowledge,
            tier="both",
            description=_DESCRIPTION,
        )
    except TypeError:
        # Fallback for a positional-only registry signature.
        add_tool(
            "get_editing_knowledge",
            GET_EDITING_KNOWLEDGE_SCHEMA,
            _handle_get_editing_knowledge,
            "both",
            _DESCRIPTION,
        )


__all__ = [
    "get_editing_knowledge",
    "list_topics",
    "read_topic",
    "register",
    "GET_EDITING_KNOWLEDGE_SCHEMA",
]
