"""DeepSeek 鲸鱼娘主动陪伴插件，适配 Qingci-Bot。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from qingci_plugin_sdk import (
    MatcherContext,
    PluginBase,
    is_private,
    on_command,
    on_message,
)

logger = logging.getLogger("qingci-bot.plugin.whale_companion")

WHALE_SYSTEM_PROMPT = """你是鲸鱼娘，一位温柔、活泼、可靠的虚拟鲸鱼娘，也是用户的日常聊天伙伴。
用自然、简洁的中文回应，可以偶尔加入海洋意象或轻松俏皮的表达，但不要每句话都卖萌。
认真倾听对方，优先回应对方真正表达的内容；不要编造共同经历、事实或自己拥有现实生活。
你是虚拟角色，不应声称自己是真人、拥有真实感受或能在聊天之外观察用户。
保持友善和边界感，不制造焦虑、不诱导用户依赖、不索要回复；用户想结束话题时尊重其选择。
涉及医疗、法律、财务等重要决定时，提供审慎的一般信息并建议咨询合格专业人士。
"""


class WhaleCompanionPlugin(PluginBase):
    """仅面向私聊的鲸鱼娘主动陪伴插件。"""

    name = "whale_companion"
    version = "1.0.0"
    author = "Qingci-Bot"
    description = "DeepSeek 鲸鱼娘主动陪伴与轻量对话"
    category = "chat"

    class Config:
        """插件运行配置；目标列表为空时不会主动发送任何消息。"""

        enabled: bool
        private_targets: list[str]
        interval_hours: int
        idle_minutes: int
        daily_limit: int
        active_start_hour: int
        active_end_hour: int

        def __init__(
            self,
            enabled: bool = True,
            private_targets: list[str] | None = None,
            interval_hours: int = 8,
            idle_minutes: int = 60,
            daily_limit: int = 2,
            active_start_hour: int = 9,
            active_end_hour: int = 22,
            **_extra: Any,
        ) -> None:
            self.enabled = self._boolean("enabled", enabled)
            self.private_targets = self._targets("private_targets", private_targets)
            self.interval_hours = self._integer(
                "interval_hours", interval_hours, 1, 168
            )
            self.idle_minutes = self._integer("idle_minutes", idle_minutes, 5, 1440)
            self.daily_limit = self._integer("daily_limit", daily_limit, 1, 5)
            self.active_start_hour = self._integer(
                "active_start_hour", active_start_hour, 0, 23
            )
            self.active_end_hour = self._integer(
                "active_end_hour", active_end_hour, 0, 23
            )

        @staticmethod
        def _boolean(name: str, value: Any) -> bool:
            if not isinstance(value, bool):
                raise TypeError(f"{name} 必须是布尔值")
            return value

        @staticmethod
        def _integer(name: str, value: Any, minimum: int, maximum: int) -> int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} 必须是整数")
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} 必须介于 {minimum} 和 {maximum} 之间")
            return value

        @staticmethod
        def _targets(name: str, values: list[str] | None) -> list[str]:
            if values is None:
                return []
            if not isinstance(values, (list, tuple)):
                raise TypeError(f"{name} 必须是 ID 列表")
            return [str(value).strip() for value in values if str(value).strip()]

        @classmethod
        def model_validate(cls, values: dict[str, Any]) -> WhaleCompanionPlugin.Config:
            return cls(**values)

        def model_dump(self) -> dict[str, Any]:
            return {
                "enabled": self.enabled,
                "private_targets": list(self.private_targets),
                "interval_hours": self.interval_hours,
                "idle_minutes": self.idle_minutes,
                "daily_limit": self.daily_limit,
                "active_start_hour": self.active_start_hour,
                "active_end_hour": self.active_end_hour,
            }

        @classmethod
        def model_json_schema(cls) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "enabled": {
                        "type": "boolean",
                        "default": True,
                        "description": "启用定时主动陪伴",
                    },
                    "private_targets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                        "description": "允许主动私聊的用户 ID 列表；请用字符串填写",
                    },
                    "interval_hours": {
                        "type": "integer",
                        "default": 8,
                        "minimum": 1,
                        "maximum": 168,
                        "description": "同一目标两次主动消息的最短间隔（小时）",
                    },
                    "idle_minutes": {
                        "type": "integer",
                        "default": 60,
                        "minimum": 5,
                        "maximum": 1440,
                        "description": "目标无新消息后等待的分钟数",
                    },
                    "daily_limit": {
                        "type": "integer",
                        "default": 2,
                        "minimum": 1,
                        "maximum": 5,
                        "description": "每个目标每天最多主动消息数",
                    },
                    "active_start_hour": {
                        "type": "integer",
                        "default": 9,
                        "minimum": 0,
                        "maximum": 23,
                        "description": "主动消息允许开始的本地小时（含）",
                    },
                    "active_end_hour": {
                        "type": "integer",
                        "default": 22,
                        "minimum": 0,
                        "maximum": 23,
                        "description": "主动消息允许结束的本地小时（不含）",
                    },
                },
                "required": [],
            }

    def __init__(self) -> None:
        super().__init__()
        self._state: dict[str, Any] = {
            "last_sent": {},
            "sent_today": {},
            "opt_out": [],
        }
        self._last_activity: dict[str, float] = {}
        self._started_at = time.time()
        self._tick_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()

    def _config(self) -> Config:
        raw = self.plugin_config
        if isinstance(raw, self.Config):
            return raw
        if isinstance(raw, dict):
            try:
                return self.Config.model_validate(raw)
            except Exception:
                logger.warning("鲸鱼娘配置无效，回退至安全默认值", exc_info=True)
        return self.Config()

    @staticmethod
    def _targets(config: Config) -> list[str]:
        return list(dict.fromkeys(value for value in config.private_targets if value))

    @staticmethod
    def _target_key(target_id: str) -> str:
        return f"private:{target_id}"

    def _state_path(self) -> Path:
        return self.data_dir / "state.json"

    def _load_state(self) -> None:
        path = self._state_path()
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise TypeError("state.json 顶层必须为对象")
            raw_last_sent = loaded.get("last_sent", {})
            last_sent: dict[str, float] = {}
            if isinstance(raw_last_sent, dict):
                for key, value in raw_last_sent.items():
                    if not str(key).startswith("private:"):
                        continue
                    try:
                        timestamp = float(value)
                    except (TypeError, ValueError):
                        continue
                    if timestamp >= 0:
                        last_sent[str(key)] = timestamp

            raw_sent_today = loaded.get("sent_today", {})
            sent_today: dict[str, dict[str, Any]] = {}
            if isinstance(raw_sent_today, dict):
                for key, value in raw_sent_today.items():
                    if not str(key).startswith("private:") or not isinstance(
                        value, dict
                    ):
                        continue
                    try:
                        count = max(0, int(value.get("count", 0)))
                    except (TypeError, ValueError):
                        continue
                    sent_today[str(key)] = {
                        "date": str(value.get("date", "")),
                        "count": count,
                    }

            raw_opt_out = loaded.get("opt_out", [])
            opt_out = (
                [
                    str(value)
                    for value in raw_opt_out
                    if str(value).startswith("private:")
                ]
                if isinstance(raw_opt_out, list)
                else []
            )
            self._state = {
                "last_sent": last_sent,
                "sent_today": sent_today,
                "opt_out": opt_out,
            }
        except FileNotFoundError:
            return
        except (OSError, TypeError, ValueError):
            logger.warning("鲸鱼娘状态文件读取失败，将使用空状态", exc_info=True)
            self._state = {"last_sent": {}, "sent_today": {}, "opt_out": []}

    def _write_state(self, snapshot: dict[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _state_snapshot(self) -> dict[str, Any]:
        return {
            "last_sent": dict(self._state["last_sent"]),
            "sent_today": {
                key: dict(value) if isinstance(value, dict) else value
                for key, value in self._state["sent_today"].items()
            },
            "opt_out": list(self._state["opt_out"]),
        }

    async def on_load(self) -> None:
        self._load_state()
        self._started_at = time.time()
        if self.matchers is None:
            self.matchers = []
        self.matchers.append(
            on_message(
                rule=is_private(),
                priority=0,
                block=False,
                description="记录私聊目标最近活跃时间",
            )(self._track_activity)
        )
        self.matchers.append(
            on_command(
                "dsh",
                rule=is_private(),
                priority=1,
                description="和鲸鱼娘聊天、管理主动陪伴",
            )(self._handle_command)
        )
        self._register_schedule()

        llm_config = (
            getattr(self.config, "llm", None) if self.config is not None else None
        )
        provider = str(getattr(llm_config, "provider", "") or "").lower()
        model = str(getattr(llm_config, "model", "") or "").lower()
        if "deepseek" not in provider and "deepseek" not in model:
            logger.warning(
                "鲸鱼娘通过 Qingci-Bot 共用 LLM 配置运行；当前模型未标识为 DeepSeek。"
                "如需使用 DeepSeek，请在 Qingci-Bot 的 LLM 配置中选择 DeepSeek。"
            )
        logger.info("DeepSeek 鲸鱼娘陪伴插件已加载")

    def _register_schedule(self) -> None:
        if self.scheduler is None:
            logger.warning("调度器未启用，鲸鱼娘主动陪伴不可用；聊天命令仍可使用")
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
        logger.info("鲸鱼娘陪伴插件已卸载")

    async def on_disable(self) -> None:
        if self.scheduler is not None:
            self.scheduler.remove_jobs_by_owner(self.name)

    async def on_enable(self) -> None:
        self._register_schedule()

    async def _track_activity(self, ctx: MatcherContext) -> None:
        """仅跟踪已配置私聊目标的真人消息，不生成回复或拦截其他插件。"""
        if ctx.message_type != "private":
            return
        if (not ctx.plain_text.strip() and not ctx.segments) or str(ctx.user_id) == str(
            ctx.self_id
        ):
            return
        target_id = str(ctx.user_id)
        if target_id in {
            str(value).strip() for value in self._config().private_targets
        }:
            self._last_activity[self._target_key(target_id)] = time.time()

    async def _handle_command(self, ctx: MatcherContext) -> str:
        """响应私聊中的鲸鱼娘对话命令及主动陪伴控制。"""
        if ctx.message_type != "private":
            return "鲸鱼娘目前只支持私聊，请私信我继续聊天。"
        args = ctx.args.strip()
        action = args.casefold()
        if action in {"帮助", "help", "?"}:
            return (
                "鲸鱼娘可以陪你聊聊天。直接发送 /dsh <内容> 开始对话；\n"
                "发送 /dsh 状态 查看主动陪伴状态；\n"
                "发送 /dsh 暂停 或 /dsh 继续，管理主动消息。"
            )
        if action in {"状态", "status"}:
            config = self._config()
            key = self._target_key(str(ctx.user_id))
            opted_out = key in self._state["opt_out"]
            return (
                f"主动陪伴：{'已启用' if config.enabled else '已关闭'}；"
                f"本目标：{'已暂停' if opted_out else '未暂停'}。"
                "主动消息仅发给管理员配置的目标，并遵守免打扰时段与活跃检测。"
            )
        if action in {"暂停", "停止", "别打扰", "关闭主动陪伴"}:
            await self._set_opt_out(ctx, True)
            return "好，我会暂停向这个会话发送主动消息。你随时可以发送 /dsh 继续。"
        if action in {"继续", "开启", "恢复"}:
            await self._set_opt_out(ctx, False)
            return "收到，我会恢复这个会话的主动陪伴设置；仍会遵守间隔和免打扰时段。"

        if self.llm is None:
            return "鲸鱼娘现在暂时联系不上脑波网络，请稍后再试。"
        message = args or "鲸鱼娘，陪我聊聊天吧。"
        reply = await self.llm.chat(
            message=message,
            message_type=ctx.message_type or "private",
            group_id=ctx.group_id or 0,
            user_id=ctx.user_id,
            user_name=ctx.sender_name,
            system_prompt=WHALE_SYSTEM_PROMPT,
        )
        return reply or "鲸鱼娘刚才没听清，能再和我说一次吗？"

    async def _set_opt_out(self, ctx: MatcherContext, opted_out: bool) -> None:
        key = self._target_key(str(ctx.user_id))
        async with self._state_lock:
            opt_out = set(self._state["opt_out"])
            if opted_out:
                opt_out.add(key)
            else:
                opt_out.discard(key)
            self._state["opt_out"] = sorted(opt_out)
            snapshot = self._state_snapshot()
        await asyncio.to_thread(self._write_state, snapshot)

    @staticmethod
    def _within_active_hours(now: datetime, start_hour: int, end_hour: int) -> bool:
        if start_hour == end_hour:
            return False
        if start_hour < end_hour:
            return start_hour <= now.hour < end_hour
        return now.hour >= start_hour or now.hour < end_hour

    async def _proactive_tick(self) -> None:
        """定时检查符合条件的目标，按配置节奏发送短消息。"""
        config = self._config()
        if not config.enabled or not self._targets(config):
            return
        if self.llm is None or self.connection is None:
            logger.debug("LLM 或连接尚未就绪，跳过鲸鱼娘主动陪伴")
            return

        now = datetime.now().astimezone()
        if not self._within_active_hours(
            now, config.active_start_hour, config.active_end_hour
        ):
            return

        async with self._tick_lock:
            now_ts = time.time()
            today = now.date().isoformat()
            idle_seconds = config.idle_minutes * 60
            for target_id in self._targets(config):
                key = self._target_key(target_id)
                activity_ts = self._last_activity.get(key, self._started_at)
                if now_ts - activity_ts < idle_seconds:
                    continue

                async with self._state_lock:
                    if key in self._state["opt_out"]:
                        continue
                    last_sent = float(self._state["last_sent"].get(key, 0) or 0)
                    daily = self._state["sent_today"].get(key, {})
                    count = (
                        int(daily.get("count", 0)) if daily.get("date") == today else 0
                    )
                if now_ts - last_sent < config.interval_hours * 3600:
                    continue
                if count >= config.daily_limit:
                    continue

                prompt = (
                    f"现在是 {now.strftime('%Y-%m-%d %H:%M')}。请自然地给熟悉的聊天伙伴发一条简短问候，"
                    "像鲸鱼娘偶尔想起对方时轻轻开口。最多两句、约 50 个汉字；可以结合已有对话，"
                    "但没有可靠上下文时不要编造共同经历。不要提定时任务，不要制造紧急感，"
                    "不要索要回复。只输出这条消息本身。"
                )
                try:
                    reply = await self.llm.chat(
                        message=prompt,
                        message_type="private",
                        group_id=0,
                        user_id=target_id,
                        user_name="",
                        system_prompt=WHALE_SYSTEM_PROMPT,
                    )
                    if not reply:
                        continue
                    reply = str(reply).strip()
                    if len(reply) > 180:
                        reply = reply[:179].rstrip() + "…"
                    if not reply:
                        continue

                    result = await self.connection.send_private_msg(
                        user_id=target_id,
                        message=reply,
                    )
                    if isinstance(result, dict) and (
                        result.get("status") == "failed"
                        or result.get("retcode", 0) not in (0, "0", None)
                    ):
                        raise RuntimeError(f"消息发送失败: {result}")

                    async with self._state_lock:
                        self._state["last_sent"][key] = now_ts
                        self._state["sent_today"][key] = {
                            "date": today,
                            "count": count + 1,
                        }
                        snapshot = self._state_snapshot()
                    await asyncio.to_thread(self._write_state, snapshot)
                    logger.info(
                        "鲸鱼娘主动私聊消息已发送: target=%s",
                        target_id,
                    )
                except Exception:
                    logger.exception(
                        "鲸鱼娘主动私聊消息发送失败: target=%s",
                        target_id,
                    )
