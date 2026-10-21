"""DeepSeek 虚拟陪伴插件，适配 Qingci-Bot。

本文件只保留插件类与 SDK 接线；配置、状态、记忆、情绪、提示词、
命令与主动发送分别位于同目录的独立模块，便于测试与维护。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

from qingci_plugin_sdk import (
    MatcherContext,
    PluginBase,
    Rule,
    is_private,
    on_command,
    on_message,
)

# 插件热重载时宿主只 reload 包入口（plugins.<name>），子模块仍留在
# sys.modules 中。按依赖顺序重新加载，避免改了子模块却仍跑旧代码。
_SUBMODULES = (
    "config",
    "emotion",
    "state",
    "diary",
    "memory",
    "persona",
    "commands",
    "companion",
    "proactive",
)


def _reload_submodules() -> None:
    for name in _SUBMODULES:
        module = sys.modules.get(f"{__name__}.{name}")
        if module is not None:
            importlib.reload(module)


_reload_submodules()

from . import commands
from . import diary as diary_ops
from .companion import CompanionEngine
from .config import CompanionConfig
from .diary import DiaryEntry, DiaryStore
from .proactive import consolidate_targets, send_due_messages
from .state import StateStore

logger = logging.getLogger("qingci-bot.plugin.whale_companion")

# 活跃消息触发的状态保存合并窗口（秒）；显式命令与主动发送仍立即落盘
_PERSIST_DELAY_SECONDS = 3.0


class WhaleCompanionPlugin(PluginBase):
    """仅面向私聊的虚拟陪伴插件。"""

    name = "whale_companion"
    version = "1.4.0"
    author = "Qingci-Bot"
    description = "DeepSeek 虚拟陪伴：私聊接管、长期记忆、情绪感知、私人日记与管理页"
    category = "chat"

    # 宿主通过 getattr(type(plugin), "Config") 读取并构造
    Config = CompanionConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: StateStore | None = None
        self._engine: CompanionEngine | None = None
        self._diary: DiaryStore | None = None
        self._started_at = time.time()
        self._tick_lock = asyncio.Lock()
        self._last_observed: dict[str, str] = {}
        self._persist_task: asyncio.Task[None] | None = None

    # ---- 运行时装配 ----

    def _config(self) -> CompanionConfig:
        raw = self.plugin_config
        if isinstance(raw, CompanionConfig):
            return raw
        if isinstance(raw, dict):
            try:
                return CompanionConfig.model_validate(raw)
            except Exception:
                logger.warning("陪伴插件配置无效，回退至安全默认值", exc_info=True)
        return CompanionConfig()

    def _ensure_runtime(self) -> CompanionEngine:
        """惰性创建状态存储与业务引擎（on_load 与测试共用同一入口）。"""
        if self._engine is not None:
            return self._engine
        if self._store is None:
            self._store = StateStore(self.data_dir / "state.json")
            self._store.load()
        if self._diary is None:
            self._diary = DiaryStore(self.data_dir / "diary.json")
            self._diary.load()
        self._engine = CompanionEngine(self._store, self._config)
        return self._engine

    @property
    def engine(self) -> CompanionEngine:
        return self._ensure_runtime()

    @staticmethod
    def _targets(config: CompanionConfig) -> list[str]:
        return list(dict.fromkeys(value for value in config.private_targets if value))

    def _persona_name(self) -> str:
        return self._config().persona_name

    def _auto_reply_rule(self) -> Rule:
        """白名单用户、非命令的普通私聊才由本插件接管。"""

        async def check(bot: Any, event: dict, ctx: MatcherContext) -> bool:
            if ctx.message_type != "private":
                return False
            text = ctx.plain_text.strip()
            if not text or text.startswith("/"):
                return False
            config = self._config()
            if not config.private_auto_reply:
                return False
            return str(ctx.user_id) in {
                str(value).strip() for value in config.private_targets
            }

        return Rule(check)

    async def _persist(self, *, force: bool = True) -> None:
        """保存状态。

        活跃消息只标记为待保存，3 秒内的多次变更合并成一次写入；
        显式命令、主动发送与卸载走强制保存，保证用户可见操作立即落盘。
        """
        if self._store is None:
            return
        if force:
            self._cancel_pending_persist()
            await self._store.save_async()
            return
        if self._persist_task is None or self._persist_task.done():
            self._persist_task = asyncio.create_task(self._delayed_persist())

    async def _persist_diary(self) -> None:
        if self._diary is not None:
            await self._diary.save_async()

    def _diary_text(self, user_id: str) -> str:
        return self._diary.latest_text(user_id) if self._diary is not None else ""

    async def _delayed_persist(self) -> None:
        try:
            await asyncio.sleep(_PERSIST_DELAY_SECONDS)
            if self._store is not None:
                await self._store.save_async()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("陪伴插件状态延迟保存失败")

    def _cancel_pending_persist(self) -> None:
        task = self._persist_task
        if task is not None and not task.done():
            task.cancel()
        self._persist_task = None

    # ---- 生命周期 ----

    async def on_load(self) -> None:
        self._ensure_runtime()
        self._started_at = time.time()
        if self.matchers is None:
            self.matchers = []
        self.matchers.append(
            on_message(
                rule=is_private(),
                priority=0,
                block=False,
                description="记录私聊目标的活跃、对话记忆与情绪",
            )(self._track_activity)
        )
        self.matchers.append(
            on_command(
                "dsh",
                rule=is_private(),
                priority=1,
                description=f"和{self._persona_name()}聊天、管理记忆与主动陪伴",
            )(self._handle_command)
        )
        self.matchers.append(
            on_message(
                rule=self._auto_reply_rule(),
                priority=10,
                block=True,
                description=f"用{self._persona_name()}人设接管白名单用户的普通私聊",
            )(self._handle_private_chat)
        )
        self._register_schedule()
        self.register_page(self._persona_name(), icon="🐋")
        self.register_api(
            "stats",
            self._api_stats,
            methods=["GET"],
            description="陪伴统计",
        )
        self._warn_if_not_deepseek()
        logger.info(
            "陪伴插件已加载（用户 %d 位，记忆 %d 条）",
            len(self._store) if self._store is not None else 0,
            sum(len(profile.facts) for profile in self._store.items())
            if self._store is not None
            else 0,
        )

    def _warn_if_not_deepseek(self) -> None:
        llm_config = (
            getattr(self.config, "llm", None) if self.config is not None else None
        )
        provider = str(getattr(llm_config, "provider", "") or "").lower()
        model = str(getattr(llm_config, "model", "") or "").lower()
        if "deepseek" not in provider and "deepseek" not in model:
            logger.warning(
                "陪伴插件通过 Qingci-Bot 共用 LLM 配置运行；当前模型未标识为 DeepSeek。"
                "如需使用 DeepSeek，请在 Qingci-Bot 的 LLM 配置中选择 DeepSeek。"
            )

    def _register_schedule(self) -> None:
        if self.scheduler is None:
            logger.warning("调度器未启用，主动陪伴不可用；聊天命令仍可使用")
            return
        self.scheduler.add_job(
            self._proactive_tick,
            trigger="interval",
            job_id="proactive_companionship",
            owner=self.name,
            minutes=5,
        )

    async def on_unload(self) -> None:
        if self.scheduler is not None:
            self.scheduler.remove_jobs_by_owner(self.name)
        self._cancel_pending_persist()
        await self._persist()
        await self._persist_diary()
        logger.info("陪伴插件已卸载")

    async def on_disable(self) -> None:
        if self.scheduler is not None:
            self.scheduler.remove_jobs_by_owner(self.name)

    async def on_enable(self) -> None:
        self._register_schedule()

    # ---- 消息记录 ----

    def _observe_user_once(self, ctx: MatcherContext, text: str) -> bool:
        """同一条消息只记录一次；返回是否真的写入了记忆。"""
        user_id = str(ctx.user_id)
        message_id = str(getattr(ctx, "message_id", "") or "")
        marker = message_id or f"text:{text}"
        if self._last_observed.get(user_id) == marker:
            return False
        self._last_observed[user_id] = marker
        self.engine.observe_user_message(user_id, text)
        return True

    async def _track_activity(self, ctx: MatcherContext) -> None:
        """仅跟踪已配置私聊目标的消息，不回复、不拦截其他插件。"""
        if ctx.message_type != "private":
            return
        text = ctx.plain_text.strip()
        if (not text and not ctx.segments) or str(ctx.user_id) == str(ctx.self_id):
            return
        # 命令文本（/dsh 或其他插件命令）不写入对话记忆；/dsh 的实际聊天
        # 内容由命令处理器记录，避免「/dsh 状态」被当成一句对话。
        if text.startswith("/"):
            return
        user_id = str(ctx.user_id)
        if user_id not in {
            str(value).strip() for value in self._config().private_targets
        }:
            return
        if self._observe_user_once(ctx, text):
            await self._persist(force=False)

    # ---- 命令处理 ----

    async def _handle_private_chat(self, ctx: MatcherContext) -> str | None:
        """白名单用户的普通私聊：用插件人设回复。"""
        text = ctx.plain_text.strip()
        if not text:
            return None
        try:
            return await self._chat_reply(ctx, text)
        except Exception:
            logger.exception("陪伴插件私聊回复失败")
            return f"{self._persona_name()}刚才走神了，能再说一次吗？"

    async def _handle_command(self, ctx: MatcherContext) -> str:
        """命令入口：异常时返回兜底文案，避免落到框架内置 chat 的人格。"""
        try:
            return await self._handle_command_impl(ctx)
        except Exception:
            logger.exception("陪伴插件命令处理失败")
            return f"{self._persona_name()}刚才走神了，能再说一次吗？"

    async def _handle_command_impl(self, ctx: MatcherContext) -> str:
        """处理私聊中的 /dsh 对话、记忆管理与主动陪伴控制。"""
        if ctx.message_type != "private":
            return f"{self._persona_name()}目前只支持私聊，请私信我继续聊天。"
        user_id = str(ctx.user_id)
        engine = self.engine
        command = commands.parse(ctx.args)

        if command.kind == "help":
            return commands.help_text(self._persona_name())

        if command.kind == "status":
            profile = engine.touch(user_id)
            text = commands.status_text(
                enabled=self._config().enabled,
                opted_out=profile.opt_out,
                emotion=engine.emotion_label(user_id),
                memory_count=len(profile.facts),
                interactions=profile.interactions,
            )
            if profile.last_error:
                text += f"\n最近一次主动发送失败：{profile.last_error}"
            return text

        if command.kind == "pause":
            engine.touch(user_id).opt_out = True
            await self._persist()
            return "好，我会暂停向这个会话发送主动消息。你随时可以发送 /dsh 继续。"

        if command.kind == "resume":
            engine.touch(user_id).opt_out = False
            await self._persist()
            return "收到，我会恢复这个会话的主动陪伴设置；仍会遵守间隔和免打扰时段。"

        if command.kind == "memory":
            return engine.memory_digest(user_id)

        if command.kind == "export":
            return commands.export_text(engine.export_memory(user_id))

        if command.kind == "diary":
            return await self._handle_diary(user_id, command.argument)

        if command.kind == "forget":
            removed = engine.forget(user_id, command.argument)
            diary_removed = 0
            if not command.argument and self._diary is not None:
                diary_removed = self._diary.clear(user_id)
                await self._persist_diary()
            await self._persist()
            if not command.argument:
                return f"已经清空 {removed} 条记忆和 {diary_removed} 篇日记。"
            return commands.forget_text(removed)

        if command.kind == "remember":
            if not command.argument:
                return "想让我记住什么？发送 /dsh 记住 <内容>。"
            engine.remember(user_id, command.argument)
            await self._persist()
            return f"记住了：{command.argument}"

        if command.kind == "diagnose":
            return self._diagnose_text(user_id)

        return await self._chat_reply(ctx, command.argument)

    async def _chat_reply(self, ctx: MatcherContext, message: str) -> str:
        """统一的对话入口：记录、调用 LLM、写回记忆与状态。"""
        user_id = str(ctx.user_id)
        engine = self.engine
        name = self._persona_name()
        if self.llm is None:
            return f"{name}现在暂时联系不上脑波网络，请稍后再试。"

        text = message.strip() or commands.default_chat_message(name)
        self._observe_user_once(ctx, text)
        reply = await self.llm.chat(
            message=text,
            message_type="private",
            group_id=0,
            user_id=user_id,
            user_name=ctx.sender_name,
            system_prompt=engine.system_prompt(
                user_id, text, diary=self._diary_text(user_id)
            ),
        )
        if not reply:
            await self._persist()
            return f"{name}刚才没听清，能再和我说一次吗？"

        reply_text = str(reply)
        engine.observe_bot_message(user_id, reply_text)
        if engine.should_consolidate(user_id):
            await engine.consolidate(user_id, self.llm)
        await self._persist()
        return reply_text

    def _diagnose_text(self, user_id: str) -> str:
        """输出人设生效情况，便于确认消息由插件而非框架 chat 处理。"""
        config = self._config()
        llm_config = (
            getattr(self.config, "llm", None) if self.config is not None else None
        )
        provider = str(getattr(llm_config, "provider", "") or "未知")
        model = str(getattr(llm_config, "model", "") or "未知")
        prompt_head = self.engine.system_prompt(
            user_id, "诊断", diary=self._diary_text(user_id)
        ).splitlines()[0][:60]
        return (
            f"插件：{self.name} v{self.version}\n"
            f"角色名：{self._persona_name()}\n"
            f"接管白名单私聊：{'开' if config.private_auto_reply else '关'}\n"
            f"当前模型：{provider}/{model}\n"
            "人设来源：插件注入（不是框架默认人格）\n"
            f"提示词开头：{prompt_head}\n"
            "若普通私聊仍按框架人格回复，请确认用户已加入 private_targets，"
            "并检查插件是否加载成功。"
        )

    # ---- 日记 ----

    async def _handle_diary(self, user_id: str, argument: str) -> str:
        if self._diary is None:
            return "日记还没有准备好。"
        action = argument.strip().casefold()
        if action in {"写", "write"}:
            entry = await self._write_diary(user_id)
            if entry is None:
                return "今天还没有足够的对话可写，或者模型暂时不可用。"
            return f"写好了：\n{entry.text}"
        if action in {"清空", "clear"}:
            removed = self._diary.clear(user_id)
            await self._persist_diary()
            return f"已经清空 {removed} 篇日记。" if removed else "本来就没有日记。"
        entries = self._diary.entries(user_id, limit=3)
        if not entries:
            return "还没有日记。今天聊过之后我会在晚上写；也可以发送 /dsh 日记 写。"
        lines = ["最近的日记："]
        for entry in reversed(entries):
            day = datetime.fromtimestamp(entry.ts, tz=timezone.utc).astimezone()
            lines.append(f"· {day.strftime('%m-%d')}（{entry.mood}）{entry.text}")
        return "\n".join(lines)

    async def _write_diary(self, user_id: str) -> DiaryEntry | None:
        if self.llm is None or self._diary is None:
            return None
        now = time.time()
        profile = self.engine.store.get(user_id, now)
        entry = await diary_ops.write_entry(
            self.llm,
            profile,
            now,
            moment=datetime.now().astimezone(),
            half_life_hours=self._config().emotion_decay_hours,
            name=self._persona_name(),
        )
        if entry is None:
            return None
        self._diary.append(user_id, entry, limit=self._config().diary_max_entries)
        await self._persist_diary()
        return entry

    async def _write_due_diaries(self, now: datetime) -> None:
        """为当天有过对话、且还没写日记的用户补写日记（每 tick 最多 3 人）。"""
        if self._diary is None or self.llm is None:
            return
        today = now.date().isoformat()
        budget = 3
        changed = False
        for profile in self.engine.store.items():
            if budget <= 0:
                break
            if today not in profile.active_dates:
                continue
            if self._diary.has_entry_on(profile.user_id, today):
                continue
            entry = await diary_ops.write_entry(
                self.llm,
                profile,
                time.time(),
                moment=now,
                half_life_hours=self._config().emotion_decay_hours,
                name=self._persona_name(),
            )
            if entry is None:
                continue
            self._diary.append(
                profile.user_id, entry, limit=self._config().diary_max_entries
            )
            budget -= 1
            changed = True
        if changed:
            await self._persist_diary()

    # ---- 主动陪伴 ----

    async def _api_stats(self, request: Any) -> dict[str, Any]:
        """管理页统计接口（由宿主挂载到 /api/plugin-web/whale_companion/stats）。"""
        return self.engine.stats()

    @staticmethod
    def _within_active_hours(now: datetime, start_hour: int, end_hour: int) -> bool:
        if start_hour == end_hour:
            return False
        if start_hour < end_hour:
            return start_hour <= now.hour < end_hour
        return now.hour >= start_hour or now.hour < end_hour

    async def _proactive_tick(self) -> None:
        """整理记忆与日记，并按配置节奏检查目标、发送主动消息。"""
        config = self._config()
        targets = self._targets(config)
        now = datetime.now().astimezone()
        now_ts = time.time()

        # 保留期清理与主动目标无关，所有用户都应遵守
        if self.engine.prune_memory() > 0:
            await self._persist()
        if self._diary is not None:
            removed = self._diary.prune(
                now_ts,
                config.memory_retention_days,
                limit=config.diary_max_entries,
            )
            if removed > 0:
                await self._persist_diary()

        if config.diary_enabled and now.hour >= config.diary_hour:
            await self._write_due_diaries(now)

        if not targets:
            return

        await consolidate_targets(self.engine, self.llm, targets, persist=self._persist)
        if not config.enabled:
            return
        if self.llm is None or self.connection is None:
            logger.debug("LLM 或连接尚未就绪，跳过主动陪伴")
            return

        if not self._within_active_hours(
            now, config.active_start_hour, config.active_end_hour
        ):
            return

        async with self._tick_lock:
            await send_due_messages(
                engine=self.engine,
                llm=self.llm,
                connection=self.connection,
                config=config,
                targets=targets,
                started_at=self._started_at,
                persist=self._persist,
                diary_provider=self._diary_text,
            )
