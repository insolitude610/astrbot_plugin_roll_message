"""Restore the trimmed ``/roll`` context after the framework reloads the conversation.

AstrBot 4.29.0 changed how a plugin-supplied ``ProviderRequest`` bound to a
conversation is prepared: ``astr_main_agent.collect_initial_request`` now
reloads the conversation from the database and overwrites ``req.contexts`` with
the persisted history, discarding the trimmed copy that :func:`roll.history.prepare_turn`
built.  The trim therefore has to be re-applied after that reload.

``on_llm_request`` fires after the reload and before the agent runner reads
``req.contexts`` (``process_stage/method/agent_sub_stages/internal.py:307``,
``agent/runners/tool_loop_agent_runner.py:315``), so the command handler records
the removed tail on the event before yielding the request and the plugin's hook
removes it again from the reloaded context.

The helpers are pure and duck-typed: they never import AstrBot and never write
the database, so the offline suite can exercise every branch.
"""

from __future__ import annotations

from typing import Any

from . import history as history_mod

#: Event extra holding the history entries that ``prepare_turn`` removed.
MARKER = "astrbot_plugin_roll_ctx"

#: ``restore`` outcomes.  ``ABSENT`` means no ``/roll`` is in flight for this
#: event; ``ALREADY`` means the context is already in the trimmed shape (older
#: AstrBot builds, or a repeated hook); ``UNMATCHED``/``INVALID`` mean the trim
#: could not be re-applied and the caller should warn.
ABSENT = "absent"
ALREADY = "already_trimmed"
RESTORED = "restored"
UNMATCHED = "unmatched"
INVALID = "invalid"


def remember(event: Any, before_history: Any, new_history: Any) -> bool:
    """Record the tail that ``/roll`` removed, for the on_llm_request hook.

    Args:
        event: The AstrMessageEvent that will carry the regeneration request.
        before_history: The conversation history as read from the database.
        new_history: The trimmed prefix that must survive in ``req.contexts``.

    Returns:
        ``True`` when the marker was stored.  Never raises: an event without the
        documented extra API simply means the hook cannot restore anything.
    """

    if not isinstance(before_history, list) or not isinstance(new_history, list):
        return False
    if len(new_history) >= len(before_history):
        return False
    removed = before_history[len(new_history) :]
    if not removed:
        return False
    setter = getattr(event, "set_extra", None)
    if not callable(setter):
        return False
    try:
        setter(MARKER, {"new": new_history, "removed": removed})
    except Exception:  # noqa: BLE001 - bookkeeping must never break the flow
        return False
    return True


def discard(event: Any) -> None:
    """Forget a pending marker, if any.  Never raises."""

    setter = getattr(event, "set_extra", None)
    if not callable(setter):
        return
    try:
        setter(MARKER, None)
    except Exception:  # noqa: BLE001
        pass


def restore(event: Any, contexts: Any) -> tuple[str, int]:
    """Remove the previously trimmed tail from ``contexts`` in place.

    The caller passes ``ProviderRequest.contexts`` as seen by the
    ``on_llm_request`` hook.  On AstrBot 4.29+ that is the full persisted
    history; on older builds it is already the trimmed history, so the function
    is a no-op there.

    Only the tail of an occurrence of the known snapshot (trimmed prefix plus
    the removed tail) is deleted, so a repeated pair elsewhere in the history
    can never be selected.  If a newer turn follows the snapshot, the history
    advanced after ``/roll`` read it and nothing is touched.

    Args:
        event: The event carrying the marker set by :func:`remember`.
        contexts: The request contexts to repair (mutated in place).

    Returns:
        ``(outcome, removed_entries)``.  Never raises.
    """

    marker = _read_marker(event)
    if marker is None:
        return ABSENT, 0
    discard(event)

    if not isinstance(contexts, list):
        return INVALID, 0

    removed = marker.get("removed")
    new_history = marker.get("new")
    if not isinstance(removed, list) or not removed:
        return ABSENT, 0
    if not isinstance(new_history, list):
        # The marker cannot be anchored to a prefix; never guess.
        return UNMATCHED, 0

    # Restore from the known snapshot: before == new_history + removed.  Only
    # the tail of an occurrence of that *exact* snapshot is trimmed, so a
    # repeated pair elsewhere in the history can never be selected.
    before = new_history + removed
    start = _last_sequence_index(contexts, before)
    if start is not None:
        history_end = start + len(before)
        if _has_newer_turn(contexts, history_end):
            # The persisted history advanced after /roll snapshotted it; the
            # snapshot's tail is no longer the newest turn.  Leave it alone.
            return UNMATCHED, 0
        tail_start = history_end - len(removed)
        del contexts[tail_start:history_end]
        return RESTORED, len(removed)

    # The snapshot is not present: either contexts are already trimmed (older
    # AstrBot, or the hook ran late) or another party rewrote them.  Use the
    # prefix as an anchor and only look for the replaced user text after it.
    prefix_at = _last_sequence_index(contexts, new_history)
    scan_from = prefix_at + len(new_history) if prefix_at is not None else 0
    if _tail_text_present_from(contexts, scan_from, removed):
        # The replaced turn is still visible under a rewritten form; warning is
        # the only safe action (deleting a guessed slice could corrupt history).
        return UNMATCHED, 0
    return ALREADY, 0


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _read_marker(event: Any) -> dict | None:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return None
    try:
        marker = getter(MARKER)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(marker, dict):
        if marker is not None:
            discard(event)
        return None
    return marker


def _has_newer_turn(entries: list, index: int) -> bool:
    for entry in entries[index:]:
        if isinstance(entry, dict) and entry.get("role") in (
            "user",
            "assistant",
            "tool",
        ):
            return True
    return False


def _last_sequence_index(haystack: list, needle: list) -> int | None:
    size = len(needle)
    if size == 0:
        return 0
    if size > len(haystack):
        return None
    for start in range(len(haystack) - size, -1, -1):
        if haystack[start : start + size] == needle:
            return start
    return None


def _tail_text_present_from(contexts: list, start: int, removed: list) -> bool:
    """Whether the replaced user text appears at or after ``start``.

    Used only to decide between "already trimmed" (no warning) and "the reload
    happened but the tail could not be matched" (warn).  Rewritten tails keep
    the user text, so substring containment is the right test.
    """

    prompt_text = _last_user_text(removed)
    if not prompt_text:
        return False
    for entry in contexts[start:]:
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        text = _content_text(entry.get("content"))
        if text and (prompt_text in text or text in prompt_text):
            return True
    return False


def _last_user_text(entries: list) -> str:
    for entry in reversed(entries):
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        text = _content_text(entry.get("content"))
        if text:
            return text
    return ""


def _content_text(content: Any) -> str:
    text, _images, _audios = history_mod.extract_content(content)
    return text
