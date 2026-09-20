"""Platform recall layer.

AstrBot 4.28.x exposes **no** message-recall API and every platform adapter
discards the handle of the message it just sent, so withdrawing a previous bot
message can only be done per platform.  This module provides:

* :class:`RecallStore` - a small, timestamped, per-session record of the
  platform message ids this AstrBot instance has sent.
* :class:`AiocqhttpBackend` - captures those ids for the ``aiocqhttp`` adapter
  (NapCat / Lagrange / LLOneBot / go-cqhttp) and deletes them on request.
* :class:`NullBackend` - silent degradation for every other platform.

Nothing here imports AstrBot; the platform object is used through duck typing.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

#: Handles sent within this many seconds of the newest one belong to the same
#: reply burst (e.g. a text-to-speech reply is delivered as voice + text).
BURST_WINDOW_SECONDS = 2.0

#: Never withdraw more than this many messages for one /roll.
MAX_BURST = 3

#: How many recent handles to keep per session.
_DEQUE_CAPACITY = 8

_ATTR_ORIGINAL = "_roll_orig_call_action"
_ATTR_WRAPPER = "_roll_wrapper_call_action"

#: OneBot send actions mapped to the chat kind they address.
SEND_ACTIONS: dict[str, str] = {
    "send_group_msg": "group",
    "send_private_msg": "private",
    "send_group_forward_msg": "group",
    "send_private_forward_msg": "private",
}

_DELETE_ACTION = "delete_msg"


@dataclass(frozen=True)
class RecallHandle:
    """A platform message that may be withdrawn."""

    message_id: Any


def routing_key(platform_id: Any, is_group: bool, session_id: Any) -> tuple[str, str, str]:
    """Build the key used to file sent messages.

    The identifiers are stringified because aiocqhttp passes ``group_id`` /
    ``user_id`` as ``int`` while ``event.get_group_id()`` /
    ``event.get_sender_id()`` return ``str``; normalising both sides is what
    makes the capture side and the lookup side meet.

    Args:
        platform_id: Platform instance id (``PlatformMetadata.id``).
        is_group: Whether the session is a group chat.
        session_id: Group id or user id.

    Returns:
        A hashable ``(platform_id, kind, session_id)`` tuple.
    """

    return (str(platform_id), "group" if is_group else "private", str(session_id))


class RecallStore:
    """Keeps the most recent sent message ids per session."""

    def __init__(
        self,
        burst_window: float = BURST_WINDOW_SECONDS,
        max_burst: int = MAX_BURST,
        capacity: int = _DEQUE_CAPACITY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._burst_window = float(burst_window)
        self._max_burst = max(1, int(max_burst))
        self._capacity = max(1, int(capacity))
        self._clock = clock
        self._data: dict[tuple, deque[tuple[float, Any]]] = {}

    def record(self, key: tuple, message_id: Any, now: float | None = None) -> None:
        """Remember a message id that this instance just sent."""

        if message_id is None:
            return
        entries = self._data.get(key)
        if entries is None:
            entries = deque(maxlen=self._capacity)
            self._data[key] = entries
        entries.append((self._clock() if now is None else float(now), message_id))

    def snapshot(self, key: tuple) -> list[RecallHandle]:
        """Return the newest reply burst as an oldest-first list of handles.

        Args:
            key: A :func:`routing_key` tuple.

        Returns:
            At most :data:`MAX_BURST` handles whose timestamps fall inside the
            burst window measured back from the newest record.
        """

        entries = self._data.get(key)
        if not entries:
            return []
        newest = max(ts for ts, _mid in entries)
        cutoff = newest - self._burst_window
        recent = sorted(
            ((ts, mid) for ts, mid in entries if ts >= cutoff),
            key=lambda item: item[0],
        )
        return [RecallHandle(message_id=mid) for _ts, mid in recent[-self._max_burst :]]

    def forget(self, key: tuple) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()


class NullBackend:
    """Recall backend for platforms without a known deletion API."""

    name = "null"

    def __init__(self, platform_name: str = "unknown", logger: Any = None) -> None:
        self.platform_name = platform_name
        self._logger = logger
        self._warned = False

    def install(self) -> None:
        """No-op: nothing to hook on an unsupported platform."""

    def uninstall(self) -> None:
        """No-op."""

    async def recall(self, handle: RecallHandle) -> bool:
        """Report that recall is unavailable without disturbing the chat flow."""

        if self._logger is not None and not self._warned:
            self._warned = True
            try:
                self._logger.debug(
                    "astrbot_plugin_roll: recall is not supported on platform "
                    f"'{self.platform_name}'; /roll will only send a new reply."
                )
            except Exception:
                pass
        return False


class AiocqhttpBackend:
    """Capture and delete sent messages for the ``aiocqhttp`` adapter.

    ``aiocqhttp.api.Api.__getattr__`` turns any unknown attribute into
    ``functools.partial(self.call_action, name)`` and ``CQHttp.send`` also ends
    up in ``call_action``, so replacing that single *instance* attribute is
    enough to observe the ``message_id`` of every outbound send.
    """

    name = "aiocqhttp"

    def __init__(self, platform_inst: Any, store: RecallStore, logger: Any = None) -> None:
        self._platform = platform_inst
        self._store = store
        self._logger = logger
        self._platform_id = _platform_id_of(platform_inst)

    # -- lifecycle -----------------------------------------------------
    def install(self) -> None:
        """Wrap ``bot.call_action``.  Safe to call repeatedly (never stacks).

        Re-installing keeps calling the *true* original recorded on the client,
        which is what makes a plugin reload safe.  A consequence worth knowing:
        if a third party wrapped ``call_action`` after we first installed, a
        re-install would bypass that third-party wrapper.  AstrBot terminates
        and re-installs plugins through the same path, so that ordering does not
        occur in practice.
        """

        bot = self._client()
        state = _instance_dict(bot)
        if state is None:
            return

        original = state.get(_ATTR_ORIGINAL)
        if not callable(original):
            original = getattr(bot, "call_action", None)
        if not callable(original):
            return

        backend = self

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await original(*args, **kwargs)
            try:
                backend._capture(args, kwargs, result)
            except Exception:  # never let bookkeeping break a send
                pass
            return result

        try:
            bot.call_action = wrapper
        except Exception:
            return
        state[_ATTR_ORIGINAL] = original
        state[_ATTR_WRAPPER] = wrapper

    def uninstall(self) -> None:
        """Restore the untouched client method."""

        bot = self._client()
        state = _instance_dict(bot)
        if state is None:
            return
        wrapper = state.get(_ATTR_WRAPPER)
        original = state.get(_ATTR_ORIGINAL)
        if wrapper is not None and state.get("call_action") is wrapper and callable(original):
            try:
                bot.call_action = original
            except Exception:
                pass
        state.pop(_ATTR_WRAPPER, None)
        state.pop(_ATTR_ORIGINAL, None)

    # -- recall --------------------------------------------------------
    async def recall(self, handle: RecallHandle) -> bool:
        """Ask the OneBot implementation to withdraw a message.

        Args:
            handle: The handle previously recorded by :meth:`install`.

        Returns:
            ``True`` when the platform accepted the deletion, ``False`` on any
            failure (including an expired QQ recall window).
        """

        bot = self._client()
        if bot is None:
            return False
        try:
            await bot.call_action(_DELETE_ACTION, message_id=handle.message_id)
            return True
        except Exception as exc:
            if self._logger is not None:
                try:
                    self._logger.debug(
                        f"astrbot_plugin_roll: recall failed ({type(exc).__name__}: {exc})"
                    )
                except Exception:
                    pass
            return False

    # -- internals -----------------------------------------------------
    def _client(self) -> Any:
        getter = getattr(self._platform, "get_client", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    def _capture(self, args: tuple, kwargs: dict, result: Any) -> None:
        action = args[0] if args else kwargs.get("action")
        if not isinstance(action, str):
            return
        message_id = _extract_message_id(result)
        if message_id is None:
            return

        if action in SEND_ACTIONS:
            is_group = SEND_ACTIONS[action] == "group"
            session_id = kwargs.get("group_id") if is_group else kwargs.get("user_id")
        elif action == "send_msg":
            if kwargs.get("group_id") is not None:
                is_group, session_id = True, kwargs.get("group_id")
            elif kwargs.get("user_id") is not None:
                is_group, session_id = False, kwargs.get("user_id")
            else:
                return
        else:
            return

        if session_id is None:
            return
        self._store.record(
            routing_key(self._platform_id, is_group, session_id),
            message_id,
        )


def make_backend(platform_inst: Any, store: RecallStore, logger: Any = None):
    """Pick the recall backend for a platform instance.

    Args:
        platform_inst: An AstrBot ``Platform`` instance.
        store: Shared :class:`RecallStore`.
        logger: Optional logger.

    Returns:
        An :class:`AiocqhttpBackend` for the OneBot adapter, otherwise a
        :class:`NullBackend` that degrades silently.
    """

    meta = getattr(platform_inst, "meta", None)
    platform_name = ""
    if callable(meta):
        try:
            platform_name = str(getattr(meta(), "name", "") or "")
        except Exception:
            platform_name = ""
    if platform_name == "aiocqhttp":
        return AiocqhttpBackend(platform_inst, store, logger)
    return NullBackend(platform_name or "unknown", logger)


def _platform_id_of(platform_inst: Any) -> str:
    meta = getattr(platform_inst, "meta", None)
    if callable(meta):
        try:
            return str(getattr(meta(), "id", "") or "")
        except Exception:
            return ""
    return ""


def _instance_dict(obj: Any) -> dict | None:
    """Return the instance ``__dict__``.

    ``getattr``/``hasattr`` must not be used on an aiocqhttp client because its
    ``__getattr__`` fabricates a callable for every unknown name.
    """

    state = getattr(obj, "__dict__", None)
    return state if isinstance(state, dict) else None


def _extract_message_id(result: Any) -> Any:
    if isinstance(result, dict):
        message_id = result.get("message_id")
    elif isinstance(result, (int, str)):
        message_id = result
    else:
        message_id = None
    if message_id is None or message_id == "":
        return None
    return message_id
