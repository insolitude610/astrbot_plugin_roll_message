"""Pure, dependency-free helpers for the /roll regeneration flow.

This module intentionally imports nothing from AstrBot so that the trimming and
prompt-rebuilding rules can be unit-tested on any OS with a bare Python
interpreter.

Background (verified against AstrBot 4.28.x):
    * A conversation history is a list of ``{"role": ..., "content": ...}``
      entries.  ``content`` is either a plain string or a list of content parts
      such as ``{"type": "text", "text": ...}``,
      ``{"type": "image_url", "image_url": {"url": ...}}``,
      ``{"type": "audio_url", "audio_url": {"url": ...}}`` and
      ``{"type": "think", ...}``.
    * A turn may contain tool calls, so the tail of a history can be
      ``[user, assistant(tool_calls), tool, ..., assistant]``.
    * The framework appends synthetic ``role="user"`` messages of its own while
      an agent run is in progress; those must never be mistaken for the user's
      own message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SYNTHETIC_USER_MARKERS: tuple[str, ...] = (
    # astrbot.core.agent.runners.tool_loop_agent_runner.ToolLoopAgentRunner.MAX_STEPS_REACHED_PROMPT
    "Maximum tool call limit reached. Stop calling tools,",
    # hard-coded literal appended by astrbot/core/astr_agent_run_util.py
    "工具调用次数已达到上限",
)


@dataclass
class PreparedTurn:
    """The outcome of trimming a history for regeneration."""

    new_history: list = field(default_factory=list)
    """History prefix that must be sent as the LLM context."""

    prompt: str = ""
    """The user message to re-send (the framework will append it after contexts)."""

    image_urls: list[str] = field(default_factory=list)
    audio_urls: list[str] = field(default_factory=list)


def extract_content(content: Any) -> tuple[str, list[str], list[str]]:
    """Split a history entry's content into text, image URLs and audio URLs.

    Args:
        content: The raw ``content`` field of a history entry.

    Returns:
        A ``(text, image_urls, audio_urls)`` tuple.  Unknown part types and
        reasoning ("think") parts are dropped on purpose.
    """
    if isinstance(content, str):
        return content.strip(), [], []

    if not isinstance(content, list):
        return "", [], []

    texts: list[str] = []
    images: list[str] = []
    audios: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
        elif part_type == "image_url":
            url = _part_url(part.get("image_url"))
            if url:
                images.append(url)
        elif part_type == "audio_url":
            url = _part_url(part.get("audio_url"))
            if url:
                audios.append(url)
        # "think" parts and unknown types are intentionally dropped.
    return "\n".join(texts), images, audios


def is_synthetic_user_entry(entry: Any, markers: tuple[str, ...] = SYNTHETIC_USER_MARKERS) -> bool:
    """Whether a user entry was injected by the framework rather than typed by the user."""

    if not isinstance(entry, dict):
        return False
    content = entry.get("content")
    if not isinstance(content, str):
        return False
    return any(marker in content for marker in markers if marker)


def prepare_turn(history: Any, markers: tuple[str, ...] = SYNTHETIC_USER_MARKERS) -> PreparedTurn | None:
    """Locate the turn to regenerate and split it into context plus prompt.

    Scans backwards for the newest genuine user entry that is followed by at
    least one assistant entry, then returns everything *before* it as the new
    context.  The entry itself becomes the prompt, which keeps the stored
    history equivalent to the original turn once the framework appends the new
    reply (the framework overwrites history with contexts + user + assistant).

    Args:
        history: Parsed conversation history (a list of entries).
        markers: Synthetic user-message markers to skip.

    Returns:
        A :class:`PreparedTurn`, or ``None`` when the history holds no turn that
        can be regenerated.
    """
    if not isinstance(history, list):
        return None

    for index in range(len(history) - 1, -1, -1):
        entry = history[index]
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        if is_synthetic_user_entry(entry, markers):
            continue
        if not _followed_by_assistant(history, index):
            continue
        prompt, images, audios = extract_content(entry.get("content"))
        if not prompt and not images and not audios:
            continue
        return PreparedTurn(
            new_history=list(history[:index]),
            prompt=prompt,
            image_urls=images,
            audio_urls=audios,
        )
    return None


def _followed_by_assistant(history: list, index: int) -> bool:
    for entry in history[index + 1 :]:
        if isinstance(entry, dict) and entry.get("role") == "assistant":
            return True
    return False


def _part_url(value: Any) -> str:
    """Accept both ``{"url": ...}`` and a bare URL string for media parts."""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        url = value.get("url")
        if isinstance(url, str):
            return url.strip()
    return ""
