"""astrbot_plugin_roll - regenerate the last reply with ``/roll``.

Wiring only: every behaviour lives in the dependency-free :mod:`roll` package.
"""

from __future__ import annotations

import importlib
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .roll import context as roll_context
from .roll import history as history_module
from .roll.core import RollService
from .roll.recall import RecallStore, make_backend

PLUGIN_NAME = "astrbot_plugin_roll"
PLUGIN_VERSION = "1.0.4"
PLUGIN_DESC = "输入 /roll 重新生成上一条回复；QQ(aiocqhttp) 下尽力撤回旧消息，其它平台自动跳过"
PLUGIN_AUTHOR = "insolitude610"
PLUGIN_REPO = "https://github.com/insolitude610/astrbot_plugin_roll_message"

#: AstrBot 4.29+ reloads the bound conversation from the database before
#: ``on_llm_request`` fires, discarding the trimmed history that /roll prepared
#: (see ``roll/context.py``).  The hook below re-applies the trim, so it has to
#: run *before* any other plugin's on_llm_request handler mutates
#: ``req.contexts`` — otherwise those plugins would see the reloaded (untrimmed)
#: history and their index-based bookkeeping would differ from pre-4.29 runs.
#: AstrBot dispatches handlers by descending priority, and every context-mutating
#: handler in the installed trees uses the default 0, so 1000 is early enough.
#: A future handler with a priority above 1000 that appends a conversation turn
#: would make the restore refuse (warning, no deletion); /roll then degrades to
#: appending a duplicate turn instead of corrupting history.
ROLL_CONTEXT_PRIORITY = 1000


def _register_restore(handler, name: str = PLUGIN_NAME):
    """Register the context-restore hook at :data:`ROLL_CONTEXT_PRIORITY`.

    The ``TypeError`` branch is defence in depth: the registrar is
    ``def register_on_llm_request(**kwargs)`` in every AstrBot tree inspected,
    but if a build rejects the keyword the plugin must still import; the hook
    then lands at priority 0, after the other on_llm_request handlers, and the
    restore becomes best-effort instead of failing the whole plugin.
    """

    try:
        return filter.on_llm_request(priority=ROLL_CONTEXT_PRIORITY)(handler)
    except TypeError:
        logger.warning(
            f"{name}: this AstrBot build rejects on_llm_request(priority=...); "
            "the /roll context restore will follow plugin load order and may "
            "miss other plugins' context edits.",
        )
        return filter.on_llm_request()(handler)


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

        # Hard dependency: without it the conversation cannot be read at all.
        # Report it instead of letting the plugin fail to load.
        conv_mgr = getattr(context, "conversation_manager", None)
        if conv_mgr is None:
            logger.error(
                f"{PLUGIN_NAME}: this AstrBot build exposes no "
                "context.conversation_manager; /roll cannot read the conversation "
                "and will stay silent.",
            )

        lock_factory = _make_lock_factory()
        active_run_checker = _make_active_run_checker()
        if lock_factory is None or active_run_checker is None:
            logger.info(
                f"{PLUGIN_NAME}: framework internals unavailable "
                f"(session lock: {'found' if lock_factory else 'missing'}, "
                f"active-run guard: {'found' if active_run_checker else 'missing'}); "
                "/roll still works, with weaker concurrency protection.",
            )

        self._service = RollService(
            config=self._config,
            conv_mgr=conv_mgr,
            store=self._store,
            backends=self._backends,
            lock_factory=lock_factory,
            active_run_checker=active_run_checker,
            logger=logger,
            synthetic_markers=_synthetic_markers(),
        )
        _warn_on_non_local_runner(context)

    async def initialize(self) -> None:
        """Install the recall backends for platforms that are already connected."""

        self._install_backends()

    @_register_restore
    async def _restore_roll_context(self, event: AstrMessageEvent, req: Any) -> None:
        """Re-apply the history trim that AstrBot 4.29 reloads away.

        ``collect_initial_request`` replaces ``req.contexts`` with the persisted
        conversation before this hook runs (``astr_main_agent.py:1507-1523`` in
        4.29.x), so the plugin removes the replaced turn again here - after the
        reload, before the runner reads the contexts and before other plugins'
        on_llm_request handlers see them.  On older AstrBot builds the contexts
        are already trimmed and this is a no-op.
        """

        outcome, removed = roll_context.restore(
            event,
            getattr(req, "contexts", None),
        )
        if outcome == roll_context.RESTORED:
            logger.debug(
                f"{PLUGIN_NAME}: restored the trimmed /roll context "
                f"({removed} entries removed again after the framework reload)",
            )
        elif outcome == roll_context.UNMATCHED:
            logger.warning(
                f"{PLUGIN_NAME}: could not match the previous turn in the "
                "request context; /roll will append a new turn instead of "
                "replacing the old one. The AstrBot request pipeline may have "
                "changed again, another plugin may have rewritten req.contexts, "
                "or the conversation advanced while /roll was queued.",
            )
        elif outcome == roll_context.INVALID:
            logger.warning(
                f"{PLUGIN_NAME}: the LLM request has no usable contexts list; "
                "/roll cannot replace the previous turn on this AstrBot build "
                "(third-party agent runner or a changed request pipeline).",
            )

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
