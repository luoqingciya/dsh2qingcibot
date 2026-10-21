"""插件配置：字段定义、校验与 WebUI 配置表单 schema。

宿主通过 ``getattr(type(plugin), "Config")`` 读取内嵌配置类，并以
``Config(**config_dict)`` 构造。因此本模块保留宽松的 ``**_extra`` 参数：
配置文件中的历史字段（例如已移除的 ``group_targets``）不会导致插件加载失败。
"""

from __future__ import annotations

from typing import Any

# 主动陪伴与记忆的默认值，集中在此便于文档与测试引用
DEFAULT_INTERVAL_HOURS = 8
DEFAULT_IDLE_MINUTES = 60
DEFAULT_DAILY_LIMIT = 2
DEFAULT_ACTIVE_START_HOUR = 9
DEFAULT_ACTIVE_END_HOUR = 22
DEFAULT_MEMORY_TURNS = 12
DEFAULT_MEMORY_FACTS = 24
DEFAULT_MEMORY_SUMMARY_EVERY = 4
DEFAULT_EMOTION_DECAY_HOURS = 12
DEFAULT_MEMORY_RETENTION_DAYS = 90
DEFAULT_DIARY_HOUR = 23
DEFAULT_DIARY_MAX_ENTRIES = 30


def _boolean(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} 必须是布尔值")
    return value


def _integer(name: str, value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须介于 {minimum} 和 {maximum} 之间")
    return value


def _targets(name: str, values: list[str] | tuple[str, ...] | None) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"{name} 必须是 ID 列表")
    return [str(value).strip() for value in values if str(value).strip()]


class CompanionConfig:
    """鲸鱼娘插件配置；私聊目标为空时不会主动发送任何消息。"""

    enabled: bool
    private_targets: list[str]
    interval_hours: int
    idle_minutes: int
    daily_limit: int
    active_start_hour: int
    active_end_hour: int
    memory_turns: int
    memory_facts: int
    memory_summary_every: int
    emotion_decay_hours: int
    memory_retention_days: int
    diary_enabled: bool
    diary_hour: int
    diary_max_entries: int

    def __init__(
        self,
        enabled: bool = True,
        private_targets: list[str] | None = None,
        interval_hours: int = DEFAULT_INTERVAL_HOURS,
        idle_minutes: int = DEFAULT_IDLE_MINUTES,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        active_start_hour: int = DEFAULT_ACTIVE_START_HOUR,
        active_end_hour: int = DEFAULT_ACTIVE_END_HOUR,
        memory_turns: int = DEFAULT_MEMORY_TURNS,
        memory_facts: int = DEFAULT_MEMORY_FACTS,
        memory_summary_every: int = DEFAULT_MEMORY_SUMMARY_EVERY,
        emotion_decay_hours: int = DEFAULT_EMOTION_DECAY_HOURS,
        memory_retention_days: int = DEFAULT_MEMORY_RETENTION_DAYS,
        diary_enabled: bool = True,
        diary_hour: int = DEFAULT_DIARY_HOUR,
        diary_max_entries: int = DEFAULT_DIARY_MAX_ENTRIES,
        **_extra: Any,
    ) -> None:
        self.enabled = _boolean("enabled", enabled)
        self.private_targets = _targets("private_targets", private_targets)
        self.interval_hours = _integer("interval_hours", interval_hours, 1, 168)
        self.idle_minutes = _integer("idle_minutes", idle_minutes, 5, 1440)
        self.daily_limit = _integer("daily_limit", daily_limit, 1, 5)
        self.active_start_hour = _integer("active_start_hour", active_start_hour, 0, 23)
        self.active_end_hour = _integer("active_end_hour", active_end_hour, 0, 23)
        self.memory_turns = _integer("memory_turns", memory_turns, 2, 40)
        self.memory_facts = _integer("memory_facts", memory_facts, 0, 100)
        self.memory_summary_every = _integer(
            "memory_summary_every", memory_summary_every, 0, 20
        )
        self.emotion_decay_hours = _integer(
            "emotion_decay_hours", emotion_decay_hours, 1, 168
        )
        self.memory_retention_days = _integer(
            "memory_retention_days", memory_retention_days, 0, 3650
        )
        self.diary_enabled = _boolean("diary_enabled", diary_enabled)
        self.diary_hour = _integer("diary_hour", diary_hour, 0, 23)
        self.diary_max_entries = _integer(
            "diary_max_entries", diary_max_entries, 5, 100
        )

    @classmethod
    def model_validate(cls, values: dict[str, Any]) -> CompanionConfig:
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
            "memory_turns": self.memory_turns,
            "memory_facts": self.memory_facts,
            "memory_summary_every": self.memory_summary_every,
            "emotion_decay_hours": self.emotion_decay_hours,
            "memory_retention_days": self.memory_retention_days,
            "diary_enabled": self.diary_enabled,
            "diary_hour": self.diary_hour,
            "diary_max_entries": self.diary_max_entries,
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
                    "default": DEFAULT_INTERVAL_HOURS,
                    "minimum": 1,
                    "maximum": 168,
                    "description": "同一用户两次主动消息的最短间隔（小时）",
                },
                "idle_minutes": {
                    "type": "integer",
                    "default": DEFAULT_IDLE_MINUTES,
                    "minimum": 5,
                    "maximum": 1440,
                    "description": "用户无新私聊消息后等待的分钟数",
                },
                "daily_limit": {
                    "type": "integer",
                    "default": DEFAULT_DAILY_LIMIT,
                    "minimum": 1,
                    "maximum": 5,
                    "description": "每个用户每天最多主动消息数",
                },
                "active_start_hour": {
                    "type": "integer",
                    "default": DEFAULT_ACTIVE_START_HOUR,
                    "minimum": 0,
                    "maximum": 23,
                    "description": "主动消息允许开始的本地小时（含）",
                },
                "active_end_hour": {
                    "type": "integer",
                    "default": DEFAULT_ACTIVE_END_HOUR,
                    "minimum": 0,
                    "maximum": 23,
                    "description": "主动消息允许结束的本地小时（不含）",
                },
                "memory_turns": {
                    "type": "integer",
                    "default": DEFAULT_MEMORY_TURNS,
                    "minimum": 2,
                    "maximum": 40,
                    "description": "每位用户保留的最近对话轮数（短期记忆）",
                },
                "memory_facts": {
                    "type": "integer",
                    "default": DEFAULT_MEMORY_FACTS,
                    "minimum": 0,
                    "maximum": 100,
                    "description": "每位用户保留的长期记忆条目上限；0 表示关闭长期记忆",
                },
                "memory_summary_every": {
                    "type": "integer",
                    "default": DEFAULT_MEMORY_SUMMARY_EVERY,
                    "minimum": 0,
                    "maximum": 20,
                    "description": "每累计多少轮用户消息触发一次记忆整理；0 表示关闭",
                },
                "emotion_decay_hours": {
                    "type": "integer",
                    "default": DEFAULT_EMOTION_DECAY_HOURS,
                    "minimum": 1,
                    "maximum": 168,
                    "description": "情绪回归基线的时间常数（小时，半衰期）",
                },
                "memory_retention_days": {
                    "type": "integer",
                    "default": DEFAULT_MEMORY_RETENTION_DAYS,
                    "minimum": 0,
                    "maximum": 3650,
                    "description": "记忆与对话保留天数；0 表示不自动清理",
                },
                "diary_enabled": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否让鲸鱼娘按日写私人日记",
                },
                "diary_hour": {
                    "type": "integer",
                    "default": DEFAULT_DIARY_HOUR,
                    "minimum": 0,
                    "maximum": 23,
                    "description": "当天几点之后可以写日记（本地时间）",
                },
                "diary_max_entries": {
                    "type": "integer",
                    "default": DEFAULT_DIARY_MAX_ENTRIES,
                    "minimum": 5,
                    "maximum": 100,
                    "description": "每位用户保留的日记篇数上限",
                },
            },
            "required": [],
        }
