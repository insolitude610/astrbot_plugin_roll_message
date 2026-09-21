"""astrbot_plugin_roll - regenerate the last reply with ``/roll``.

Wiring only: every behaviour lives in the dependency-free :mod:`roll` package.
"""

from __future__ import annotations

import importlib
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .roll import history as history_module
from .roll.core import RollService
from .roll.recall import RecallStore, make_backend

PLUGIN_NAME = "astrbot_plugin_roll"
PLUGIN_VERSION = "1.0.0"
PLUGIN_DESC = "输入 /roll 重新生成上一条回复；QQ(aiocqhttp) 下尽力撤回旧消息，其它平台自动跳过"
PLUGIN_AUTHOR = "insolitude610"
PLUGIN_REPO = "https://github.com/insolitude610/astrbot_plugin_roll_message"


def _load_symbol(module_name: str, attr_name: str) -> Any:
    """Best-effort import of a framework internal, never raises."""

    try:
        module = importlib.import_module(module_name)
    except Exception:
        return None
    return getattr(module, attr_name, None)


def _make_lock_factory():
    """Return a factory for the framework's own per-session lock.

    Serialising the history read with the framework's generation lock closes the
    window in which a concurrent reply could be written and then overwritten by
    our regeneration.
    """

    manager = _load_symbol("astrbot.core.utils.session_lock", "session_lock_manager")
    if manager is None or not hasattr(manager, "acquire_lock"):
        return None
    return lambda umo: manager.acquire_lock(umo)


def _make_active_run_checker():
    """Detect an in-flight agent run for a session (follow-up capture guard)."""

    runners = _load_symbol(
        "astrbot.core.pipeline.process_stage.follow_up",
        "_ACTIVE_AGENT_RUNNERS",
    )
    if not isinstance(runners, dict):
        return None
    return lambda umo: runners.get(umo)


def _synthetic_markers() -> tuple[str, ...]:
    """Synthetic user-message markers, including the framework's own constant."""

    markers = list(history_module.SYNTHETIC_USER_MARKERS)
    runner_cls = _load_symbol(
        "astrbot.core.agent.runners.tool_loop_agent_runner",
        "ToolLoopAgentRunner",
    )
    prompt = getattr(runner_cls, "MAX_STEPS_REACHED_PROMPT", None)
    if isinstance(prompt, str) and prompt and prompt not in markers:
        markers.append(prompt)
    return tuple(markers)


def _warn_on_non_local_runner(context: Context) -> None:
    """``/roll`` relies on the local agent runner writing conversation history."""

    try:
        config = context.get_config()
        runner = config.get("agent_runner", {})
        runner_type = (
            str(runner.get("runner_type", "local")) if isinstance(runner, dict) else "local"
        )
    except Exception:
        return
    if runner_type != "local":
        logger.warning(
            f"{PLUGIN_NAME}: agent_runner.runner_type={runner_type!r}; /roll expects the "
            "local runner, regeneration may not be recorded in the conversation.",
        )


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class RollPlugin(Star):
    """``/roll`` - regenerate the last reply and withdraw the old platform message."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self._config = config if config is not None else {}
        self._store = RecallStore()
        self._backends: dict[str, Any] = {}
        self._service = RollService(
            config=self._config,
            conv_mgr=context.conversation_manager,
            store=self._store,
            backends=self._backends,
            lock_factory=_make_lock_factory(),
            active_run_checker=_make_active_run_checker(),
            logger=logger,
            synthetic_markers=_synthetic_markers(),
        )
        _warn_on_non_local_runner(context)

    async def initialize(self) -> None:
        """Install the recall backends for platforms that are already connected."""

        self._install_backends()

    @filter.on_platform_loaded()
    async def _on_platform_loaded(self) -> None:
        """Install the recall backend for a platform that connected later."""

        self._install_backends()

    @filter.command("roll", alias={"reroll", "重roll"})
    async def roll(self, event: AstrMessageEvent):
        """Regenerate the previous reply of the current conversation."""

        async for item in self._service.handle(event):
            yield item

    async def terminate(self) -> None:
        """Restore every patched platform client."""

        for backend in list(self._backends.values()):
            try:
                backend.uninstall()
            except Exception:
                logger.error(f"{PLUGIN_NAME}: failed to restore a platform client")

    # ------------------------------------------------------------------
    def _install_backends(self) -> None:
        try:
            instances = list(self.context.platform_manager.get_insts())
        except Exception:
            instances = []

        for instance in instances:
            try:
                meta = instance.meta()
                platform_id = str(getattr(meta, "id", "") or "")
            except Exception:
                continue
            if not platform_id or platform_id in self._backends:
                continue
            backend = make_backend(instance, self._store, logger)
            try:
                backend.install()
            except Exception:
                logger.error(f"{PLUGIN_NAME}: failed to hook platform {platform_id}")
                continue
            self._backends[platform_id] = backend
