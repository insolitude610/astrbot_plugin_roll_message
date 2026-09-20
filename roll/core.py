"""Orchestration for the ``/roll`` command.

The service is deliberately duck-typed: it talks to the message event, the
conversation manager, the recall store and the recall backends through a very
small surface, so it can be unit-tested without AstrBot and stays identical on
every operating system.

Flow for one ``/roll`` (see also the module docstring of :mod:`roll.history`):

1. Forbid the framework's default LLM request for this event.
2. Check permission, cooldown and the in-flight/active-run guards.
3. Under the framework's own per-session lock, read the conversation, trim the
   last turn into ``contexts`` + ``prompt``, build a *copy* of the conversation
   carrying the trimmed history, and snapshot the platform message(s) to
   withdraw.  The database is never written here: the framework rewrites the
   conversation itself after a successful run, so a failed run leaves the
   previous turn intact.
4. Withdraw those messages immediately, *before* generating - the old message
   should disappear the moment the user asks, rather than lingering until the
   new reply arrives.  The documented trade-off: if the generation then fails,
   the old message is already gone.
5. Yield ``event.request_llm(...)`` so the normal AstrBot pipeline generates and
   delivers the reply.
6. After the yield (the pipeline is an onion, so the reply has been sent and the
   run's history persisted by then) log a warning when the conversation does not
   show a new turn for this roll - i.e. the generation probably failed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import history as history_mod
from .recall import NullBackend, RecallStore, routing_key

#: How long to wait for the framework's per-session lock before giving up.
LOCK_TIMEOUT_SECONDS = 8.0

#: An in-flight marker older than this is considered stale and ignored.
INFLIGHT_TTL_SECONDS = 180.0

#: Cooldown bookkeeping is pruned once it tracks more than this many sessions,
#: and entries older than this many seconds are dropped.
_MAX_TRACKED_SESSIONS = 256
_PRUNE_KEEP_SECONDS = 300.0

DEFAULT_COOLDOWN_SECONDS = 3
DEFAULT_PERMISSION_MODE = "admin"
NO_REPLY_TIP = "当前没有可以重新生成的回复。"

#: Internal outcomes of the preparation step.
_EMPTY = "empty"  # nothing to regenerate -> optional tip
_SILENT = None  # busy / locked / active run -> stay quiet


@dataclass
class _Prepared:
    """Everything needed to issue the regeneration request."""

    conv_copy: Any
    new_history: list
    before_history: list
    prompt: str
    image_urls: list
    audio_urls: list
    cid: str
    handles: list
    recall_key: tuple | None = None


class _ConversationCopy:
    """Attribute-delegating copy used when the conversation is not a dataclass."""

    def __init__(self, conv: Any, history: str) -> None:
        object.__setattr__(self, "_conv", conv)
        object.__setattr__(self, "history", history)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_conv"), name)


class RollService:
    """Implements the ``/roll`` behaviour for one plugin instance."""

    def __init__(
        self,
        *,
        config: Any = None,
        conv_mgr: Any,
        store: RecallStore | None = None,
        backends: dict | None = None,
        lock_factory: Callable[[str], Any] | None = None,
        active_run_checker: Callable[[str], Any] | None = None,
        logger: Any = None,
        now: Callable[[], float] | None = None,
        synthetic_markers: tuple[str, ...] | None = None,
    ) -> None:
        self._config = config if config is not None else {}
        self._conv_mgr = conv_mgr
        self._store = store if store is not None else RecallStore()
        self._backends = backends if backends is not None else {}
        self._lock_factory = lock_factory
        self._active_run_checker = active_run_checker
        self._logger = logger
        self._now = now if now is not None else time.monotonic
        self._markers = (
            tuple(synthetic_markers)
            if synthetic_markers
            else history_mod.SYNTHETIC_USER_MARKERS
        )
        self._inflight: dict[str, float] = {}
        self._last_at: dict[str, float] = {}

    # ------------------------------------------------------------------
    # handler entry point (async generator)
    # ------------------------------------------------------------------
    async def handle(self, event: Any):
        """Run one ``/roll``; yields at most one item for the caller to yield."""

        # 1. Never let the framework answer the literal command text.
        self._forbid_default_llm(event)

        umo = getattr(event, "unified_msg_origin", None)
        if not umo:
            return

        if not self._permitted(event):
            return

        now = self._now()
        # Prune unconditionally: _last_at is written on every accepted roll, so
        # the bookkeeping must not depend on the cooldown being enabled.
        self._prune_timestamps(now, keep_for=max(self._cooldown(), _PRUNE_KEEP_SECONDS))
        if self._is_busy(umo, now) or self._is_cooling_down(umo, now):
            return

        self._inflight[umo] = now
        self._last_at[umo] = now
        try:
            outcome = await self._prepare(event, umo)
            if isinstance(outcome, _Prepared):
                self._log(
                    "info",
                    f"/roll on {umo}: regenerating with {len(outcome.new_history)} "
                    f"context entries, {len(outcome.handles)} recall candidate(s)",
                )
                # Withdraw the old message FIRST so it disappears the moment the
                # user asks, instead of lingering until the new reply arrives.
                await self._withdraw_old(event, umo, outcome)
                yield self._build_request(event, outcome)
                if not await self._history_advanced(umo, outcome):
                    self._log(
                        "warning",
                        "the previous message was withdrawn but the conversation "
                        "gained no new turn; this generation probably failed",
                    )
                return
            if outcome == _EMPTY:
                self._log("debug", f"/roll on {umo}: nothing to regenerate")
                if self._tips_enabled():
                    yield self._make_tip(event)
        finally:
            self._inflight.pop(umo, None)

    # ------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------
    def _forbid_default_llm(self, event: Any) -> None:
        try:
            event.should_call_llm(True)
        except Exception:
            pass

    def _permitted(self, event: Any) -> bool:
        if self._permission_mode() == "everyone":
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _is_busy(self, umo: str, now: float) -> bool:
        started = self._inflight.get(umo)
        if started is None:
            return False
        if now - started >= INFLIGHT_TTL_SECONDS:
            self._inflight.pop(umo, None)
            return False
        return True

    def _is_cooling_down(self, umo: str, now: float) -> bool:
        cooldown = self._cooldown()
        if cooldown <= 0:
            return False
        last = self._last_at.get(umo)
        if last is None:
            return False
        return (now - last) < cooldown

    def _prune_timestamps(self, now: float, keep_for: float) -> None:
        """Keep the cooldown map from growing without bound across sessions.

        ``_MAX_TRACKED_SESSIONS`` is a trigger rather than a hard cap: once the
        map is larger, every entry older than ``keep_for`` is dropped.  Live
        cooldowns are therefore never discarded.
        """

        if len(self._last_at) <= _MAX_TRACKED_SESSIONS:
            return
        cutoff = now - keep_for
        for key in [k for k, ts in self._last_at.items() if ts < cutoff]:
            self._last_at.pop(key, None)

    # ------------------------------------------------------------------
    # preparation (runs under the framework's per-session lock)
    # ------------------------------------------------------------------
    async def _prepare(self, event: Any, umo: str):
        if self._active_run_checker is not None:
            try:
                if self._active_run_checker(umo):
                    return _SILENT
            except Exception:
                pass

        lock_cm = self._make_lock(umo)
        if lock_cm is None:
            return await self._read_and_trim(event, umo)

        try:
            await asyncio.wait_for(lock_cm.__aenter__(), LOCK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            # wait_for cancelled __aenter__; the lock manager's own finally has
            # already released, so there is nothing to clean up here.
            return _SILENT
        except Exception:
            return await self._read_and_trim(event, umo)

        try:
            return await self._read_and_trim(event, umo)
        finally:
            try:
                await lock_cm.__aexit__(None, None, None)
            except Exception:
                pass

    def _make_lock(self, umo: str) -> Any:
        if self._lock_factory is None:
            return None
        try:
            return self._lock_factory(umo)
        except Exception:
            return None

    async def _read_and_trim(self, event: Any, umo: str):
        try:
            cid = await self._conv_mgr.get_curr_conversation_id(umo)
        except Exception:
            return _SILENT
        if not cid:
            return _EMPTY

        try:
            conv = await self._conv_mgr.get_conversation(umo, cid)
        except Exception:
            return _SILENT
        if conv is None:
            return _EMPTY

        raw = getattr(conv, "history", "") or "[]"
        try:
            before_history = json.loads(raw)
        except (TypeError, ValueError):
            return _SILENT
        if not isinstance(before_history, list):
            return _SILENT

        parsed = history_mod.prepare_turn(before_history, self._markers)
        if parsed is None:
            return _EMPTY

        snapshot = self._snapshot(event)
        prepared = _Prepared(
            conv_copy=self._copy_conversation(
                conv, json.dumps(parsed.new_history, ensure_ascii=False)
            ),
            new_history=parsed.new_history,
            before_history=before_history,
            prompt=parsed.prompt,
            image_urls=parsed.image_urls,
            audio_urls=parsed.audio_urls,
            cid=cid,
            handles=snapshot[0],
            recall_key=snapshot[1],
        )
        return prepared

    def _copy_conversation(self, conv: Any, history_json: str) -> Any:
        try:
            return dataclasses.replace(conv, history=history_json)
        except Exception:
            return _ConversationCopy(conv, history_json)

    def _snapshot(self, event: Any) -> tuple[list, tuple | None]:
        """The burst of messages to withdraw, plus the key they were filed under."""

        try:
            group_id = event.get_group_id()
            is_group = bool(group_id)
            session_id = group_id if is_group else event.get_sender_id()
            key = routing_key(event.get_platform_id(), is_group, session_id)
        except Exception:
            return [], None
        try:
            return self._store.snapshot(key), key
        except Exception:
            return [], None

    # ------------------------------------------------------------------
    # request / tip construction
    # ------------------------------------------------------------------
    def _build_request(self, event: Any, prepared: _Prepared) -> Any:
        return event.request_llm(
            prompt=prepared.prompt,
            image_urls=list(prepared.image_urls),
            audio_urls=list(prepared.audio_urls),
            conversation=prepared.conv_copy,
        )

    def _make_tip(self, event: Any) -> Any:
        return event.plain_result(NO_REPLY_TIP)

    # ------------------------------------------------------------------
    # recall (runs as soon as the roll is accepted, before generating)
    # ------------------------------------------------------------------
    async def _withdraw_old(self, event: Any, umo: str, prepared: _Prepared) -> None:
        """Withdraw the message the user wants replaced.

        This happens before the regeneration is requested so the old message
        disappears immediately.  The trade-off is explicit: if the generation
        then fails, the old message is already gone and only the framework's
        error text remains (the failure is logged below by ``_history_advanced``).
        """

        if not self._recall_enabled():
            self._log("debug", "recall skipped: recall_old is disabled")
            return
        if not prepared.handles:
            self._log(
                "debug",
                "recall skipped: no sent message was captured for this session "
                "(platform not hooked, or the reply predates the plugin)",
            )
            return

        backend = self._backend_for(event)
        if backend is None or getattr(backend, "name", "") == "null":
            self._log("debug", "recall skipped: no recall support on this platform")
            return

        recalled = 0
        failed = 0
        try:
            for handle in prepared.handles:
                try:
                    ok = await backend.recall(handle)
                except Exception:  # recall must never disturb the chat flow
                    ok = False
                if ok:
                    recalled += 1
                else:
                    failed += 1
            self._log(
                "info",
                f"recall on {umo}: {recalled} withdrawn, {failed} failed "
                "(an expired platform window or missing permission is expected)",
            )
        finally:
            # These ids have had their chance; a later /roll must not try to
            # delete an already-deleted message just because it is still inside
            # the burst window.
            if prepared.recall_key is not None:
                self._store.forget(prepared.recall_key)

    def _backend_for(self, event: Any) -> Any:
        try:
            return self._backends.get(str(event.get_platform_id()))
        except Exception:
            return None

    async def _history_advanced(self, umo: str, prepared: _Prepared) -> bool:
        """Whether the run really persisted a new turn.

        A provider error writes nothing, so an unchanged history means "do not
        delete the old message".  Beyond that we look for the newest user turn
        and require that it now carries an assistant reply and that its text is
        related to the prompt we rebuilt.  Exact equality with the trimmed
        prefix is deliberately only a fallback: other plugins may decorate the
        prompt before it is stored (for example by prepending a perception
        header) and history compression may rewrite the prefix entirely, so a
        byte-exact comparison would silently disable recall.
        """

        try:
            conv = await self._conv_mgr.get_conversation(umo, prepared.cid)
        except Exception:
            return False
        if conv is None:
            return False
        try:
            stored = json.loads(getattr(conv, "history", "") or "[]")
        except (TypeError, ValueError):
            return False
        if not isinstance(stored, list) or stored == prepared.before_history:
            return False
        return _tail_shows_our_turn(stored, prepared)

    # ------------------------------------------------------------------
    # configuration helpers
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self._config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _permission_mode(self) -> str:
        try:
            return str(self._cfg("permission_mode", DEFAULT_PERMISSION_MODE)).strip().lower()
        except Exception:
            return DEFAULT_PERMISSION_MODE

    def _cooldown(self) -> float:
        try:
            return max(0.0, float(self._cfg("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)))
        except (TypeError, ValueError):
            return float(DEFAULT_COOLDOWN_SECONDS)

    def _recall_enabled(self) -> bool:
        return bool(self._cfg("recall_old", True))

    def _tips_enabled(self) -> bool:
        return bool(self._cfg("show_tip_when_no_reply", True))

    def _log(self, level: str, message: str) -> None:
        """Emit one prefixed line; logging must never affect the chat flow."""

        if self._logger is None:
            return
        try:
            getattr(self._logger, level)(f"astrbot_plugin_roll: {message}")
        except Exception:
            pass


def _role(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("role") or "")
    return ""


def _prompt_text(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    text, _images, _audios = history_mod.extract_content(entry.get("content"))
    return text


def _last_user_index(history: list) -> int | None:
    """Index of the newest user entry, or None."""

    for index in range(len(history) - 1, -1, -1):
        if _role(history[index]) == "user":
            return index
    return None


def _tail_shows_our_turn(stored: list, prepared: _Prepared) -> bool:
    """Whether the stored history ends with a user turn that answers our prompt.

    This only drives a diagnostic log line (the withdrawal itself already
    happened), so it errs towards *not* warning: it accepts the turn either when
    the newest user entry sits where our trimmed prefix ends and its text still
    carries the prompt we rebuilt, or when the text merely contains that prompt
    (history compression may have rewritten the prefix and other plugins may have
    decorated the prompt before it was stored).
    """

    index = _last_user_index(stored)
    if index is None:
        return False
    if not any(_role(entry) == "assistant" for entry in stored[index + 1 :]):
        return False

    tail_text = _prompt_text(stored[index])
    if _contains(tail_text, prepared.prompt):
        return True

    prefix_len = len(prepared.new_history)
    return (
        prefix_len > 0
        and index == prefix_len
        and stored[:prefix_len] == prepared.new_history
    )


def _contains(stored_text: str, prompt: str) -> bool:
    if not stored_text or not prompt:
        return False
    return prompt in stored_text or stored_text in prompt


__all__ = [
    "RollService",
    "NullBackend",
    "RecallStore",
    "NO_REPLY_TIP",
    "LOCK_TIMEOUT_SECONDS",
    "INFLIGHT_TTL_SECONDS",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_PERMISSION_MODE",
]
