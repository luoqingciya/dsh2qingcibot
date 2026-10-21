"""陪伴业务逻辑：把状态、记忆、情绪与提示词组合成可测试的纯逻辑层。

本模块不依赖 Qingci-Bot SDK，插件入口只负责事件接线与消息发送，
便于单元测试与后续替换宿主。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import memory as memory_ops
from .config import CompanionConfig
from .emotion import apply_observation, decay, label
from .memory import add_fact, add_turn, extract_facts, memory_digest, normalize
from .persona import build_proactive_prompt, build_system_prompt
from .state import StateStore, UserState

logger = logging.getLogger("qingci-bot.plugin.whale_companion.engine")

# 主动发送失败后的退避：5 分钟、15 分钟、60 分钟；连续失败进入 6 小时冷却
_RETRY_DELAYS = (5 * 60, 15 * 60, 60 * 60)
_MAX_SEND_FAILURES = 3
_FAILURE_COOLDOWN_SECONDS = 6 * 3600


def _local_moment(timestamp: float) -> datetime:
    """把时间戳转换为本地时区时间，供提示词描述当前时段。"""
    return datetime.fromtimestamp(timestamp).astimezone()


class CompanionEngine:
    """陪伴状态机：记录互动、维护记忆与情绪、判断主动消息时机。"""

    def __init__(
        self,
        store: StateStore,
        config_getter: Callable[[], CompanionConfig],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self._config_getter = config_getter
        self._clock = clock

    @property
    def config(self) -> CompanionConfig:
        return self._config_getter()

    def now(self) -> float:
        return self._clock()

    # ---- 互动记录 ----

    def touch(self, user_id: str, now: float | None = None) -> UserState:
        """记录一次活跃，不改变记忆与情绪。"""
        timestamp = self.now() if now is None else now
        profile = self.store.get(user_id, timestamp)
        profile.last_seen = timestamp
        return profile

    def observe_user_message(
        self, user_id: str, text: str, now: float | None = None
    ) -> UserState:
        """记录用户消息：更新短期记忆、长期事实与情绪。"""
        timestamp = self.now() if now is None else now
        config = self.config
        profile = self.store.get(user_id, timestamp)
        if profile.first_seen <= 0:
            profile.first_seen = timestamp
        profile.last_seen = timestamp
        profile.interactions += 1
        profile.pending_turns += 1
        profile.mark_active(timestamp)
        profile.emotion = apply_observation(
            profile.emotion, text, timestamp, config.emotion_decay_hours
        )
        add_turn(profile, "user", text, timestamp, config.memory_turns)
        for fact in extract_facts(text):
            add_fact(profile, fact, timestamp, config.memory_facts)
        return profile

    def observe_bot_message(
        self, user_id: str, text: str, now: float | None = None
    ) -> None:
        """记录鲸鱼娘发出的内容，供下次承接话题。"""
        timestamp = self.now() if now is None else now
        profile = self.store.get(user_id, timestamp)
        add_turn(profile, "assistant", text, timestamp, self.config.memory_turns)

    def remember(self, user_id: str, text: str, now: float | None = None) -> bool:
        """显式记住一条信息（对应 /dsh 记住）。"""
        timestamp = self.now() if now is None else now
        content = normalize(text)
        if not content:
            return False
        profile = self.store.get(user_id, timestamp)
        return add_fact(profile, content, timestamp, self.config.memory_facts)

    def forget(self, user_id: str, keyword: str = "") -> int:
        profile = self.store.get(user_id, self.now())
        return memory_ops.forget(profile, keyword)

    def memory_digest(self, user_id: str) -> str:
        profile = self.store.get(user_id, self.now())
        return memory_digest(profile)

    # ---- 提示词 ----

    def system_prompt(
        self,
        user_id: str,
        query: str,
        now: float | None = None,
        *,
        diary: str = "",
    ) -> str:
        timestamp = self.now() if now is None else now
        profile = self.store.get(user_id, timestamp)
        return build_system_prompt(
            profile,
            query,
            timestamp,
            moment=_local_moment(timestamp),
            half_life_hours=self.config.emotion_decay_hours,
            max_facts=min(6, self.config.memory_facts),
            diary=diary,
        )

    def proactive_prompt(
        self, user_id: str, now: float | None = None, *, diary: str = ""
    ) -> tuple[str, str]:
        timestamp = self.now() if now is None else now
        profile = self.store.get(user_id, timestamp)
        return build_proactive_prompt(
            profile,
            timestamp,
            moment=_local_moment(timestamp),
            half_life_hours=self.config.emotion_decay_hours,
            max_facts=min(4, self.config.memory_facts),
            diary=diary,
        )

    def emotion_label(self, user_id: str, now: float | None = None) -> str:
        timestamp = self.now() if now is None else now
        profile = self.store.get(user_id, timestamp)
        return label(decay(profile.emotion, timestamp, self.config.emotion_decay_hours))

    # ---- 主动消息时机 ----

    def can_send(
        self,
        user_id: str,
        now: float,
        today: str,
        activity_ts: float | None = None,
    ) -> bool:
        """判断是否可以向该用户主动发送消息。"""
        config = self.config
        if not config.enabled:
            return False
        profile = self.store.get(user_id, now)
        if profile.opt_out:
            return False
        if profile.retry_after and now < profile.retry_after:
            return False
        last_active = activity_ts if activity_ts is not None else profile.last_seen
        if last_active and now - last_active < config.idle_minutes * 60:
            return False
        if profile.last_sent and now - profile.last_sent < config.interval_hours * 3600:
            return False
        return not (
            profile.sent_date == today and profile.sent_count >= config.daily_limit
        )

    def mark_sent(self, user_id: str, now: float, today: str) -> None:
        profile = self.store.get(user_id, now)
        profile.last_sent = now
        if profile.sent_date == today:
            profile.sent_count += 1
        else:
            profile.sent_date = today
            profile.sent_count = 1

    def mark_send_success(self, user_id: str, now: float) -> None:
        profile = self.store.get(user_id, now)
        profile.failure_count = 0
        profile.retry_after = 0.0
        profile.last_error = ""

    def mark_send_failure(self, user_id: str, now: float, error: str) -> None:
        """记录失败并计算退避时间；连续失败后进入较长冷却。"""
        profile = self.store.get(user_id, now)
        profile.failure_count += 1
        profile.last_error = error[:200]
        if profile.failure_count >= _MAX_SEND_FAILURES:
            delay = _FAILURE_COOLDOWN_SECONDS
        else:
            index = min(profile.failure_count - 1, len(_RETRY_DELAYS) - 1)
            delay = _RETRY_DELAYS[index]
        profile.retry_after = now + delay

    # ---- 记忆整理 ----

    def should_consolidate(self, user_id: str) -> bool:
        config = self.config
        if config.memory_summary_every <= 0 or config.memory_facts <= 0:
            return False
        profile = self.store.get(user_id, self.now())
        return (
            profile.pending_turns >= config.memory_summary_every
            and len(profile.turns) >= 2
        )

    async def consolidate(self, user_id: str, llm: Any) -> bool:
        """按需调用模型整理记忆；失败时保留本地规则抽取的结果。"""
        if not self.should_consolidate(user_id):
            return False
        profile = self.store.get(user_id, self.now())
        return await memory_ops.consolidate(
            llm, profile, self.now(), max_facts=self.config.memory_facts
        )

    # ---- 保留期与统计 ----

    def prune_memory(self, now: float | None = None) -> int:
        """按保留期清理所有用户的过期记忆，返回删除条数。"""
        retention_days = self.config.memory_retention_days
        if retention_days <= 0:
            return 0
        timestamp = self.now() if now is None else now
        return sum(
            profile.prune_memory(timestamp, retention_days)
            for profile in self.store.items()
        )

    def export_memory(self, user_id: str) -> dict[str, Any]:
        profile = self.store.get(user_id, self.now())
        return profile.export_memory()

    def stats(self, now: float | None = None) -> dict[str, Any]:
        """管理页概览：只输出聚合数据与脱敏用户行。"""
        timestamp = self.now() if now is None else now
        today = datetime.fromtimestamp(timestamp).astimezone().date().isoformat()
        profiles = self.store.items()
        rows = []
        for profile in sorted(profiles, key=lambda item: item.last_seen, reverse=True):
            rows.append(
                {
                    "user_id": _mask_id(profile.user_id),
                    "last_seen": _iso_time(profile.last_seen),
                    "interactions": profile.interactions,
                    "facts": len(profile.facts),
                    "emotion": label(
                        decay(
                            profile.emotion, timestamp, self.config.emotion_decay_hours
                        )
                    ),
                    "paused": profile.opt_out,
                    "failure_count": profile.failure_count,
                    "last_error": profile.last_error,
                }
            )
        return {
            "generated_at": _iso_time(timestamp),
            "users": len(profiles),
            "facts": sum(len(profile.facts) for profile in profiles),
            "paused": sum(1 for profile in profiles if profile.opt_out),
            "sent_today": sum(
                profile.sent_count if profile.sent_date == today else 0
                for profile in profiles
            ),
            "failing": sum(1 for profile in profiles if profile.failure_count > 0),
            "rows": rows[:100],
        }


def _mask_id(user_id: str) -> str:
    if len(user_id) <= 4:
        return "***"
    return f"{user_id[:2]}***{user_id[-2:]}"


def _iso_time(timestamp: float) -> str:
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M")
