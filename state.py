"""用户状态持久化：短期对话、长期记忆、情绪与主动发送记录。

文件为 ``<data_dir>/state.json``，采用「临时文件 + 原子替换」写入，避免
进程中断留下半截 JSON。旧版（v1，顶层扁平结构）状态会在加载时迁移，
其中群聊相关记录会被丢弃——插件只服务私聊。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .emotion import EmotionState

logger = logging.getLogger("qingci-bot.plugin.whale_companion.state")

SCHEMA_VERSION = 2
_PRIVATE_PREFIX = "private:"
_MAX_TURN_CHARS = 500
_MAX_USERS = 500
_MAX_ACTIVE_DATES = 120

# 单值槽位：同一槽位只保留最新事实，避免「住在杭州」与「搬到上海」并存。
# 偏好、计划等可以同时存在多条，不设槽位。
_SLOT_PREFIXES = (
    ("名字是", "name"),
    ("希望被称作", "name"),
    ("来自", "location"),
    ("生日是", "birthday"),
    ("在 ", "occupation"),
)


def infer_slot(text: str) -> str:
    """从事实文本前缀推断单值槽位；无法判断时返回空串。"""
    content = text.strip()
    for prefix, slot in _SLOT_PREFIXES:
        if content.startswith(prefix):
            return slot
    return ""


@dataclass
class Turn:
    """一条短期对话记录。"""

    role: str
    text: str
    ts: float

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "text": self.text, "ts": self.ts}

    @classmethod
    def from_dict(cls, data: Any) -> Turn | None:
        if not isinstance(data, dict):
            return None
        role = str(data.get("role", ""))
        text = str(data.get("text", "")).strip()
        if role not in {"user", "assistant"} or not text:
            return None
        return cls(role=role, text=text[:_MAX_TURN_CHARS], ts=_as_float(data.get("ts")))


@dataclass
class Fact:
    """一条长期记忆；``slot`` 用于单值信息的覆盖更新。"""

    text: str
    ts: float
    hits: int = 1
    slot: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "ts": self.ts, "hits": self.hits, "slot": self.slot}

    @classmethod
    def from_dict(cls, data: Any) -> Fact | None:
        if not isinstance(data, dict):
            return None
        text = str(data.get("text", "")).strip()
        if not text:
            return None
        text = text[:120]
        return cls(
            text=text,
            ts=_as_float(data.get("ts")),
            hits=max(1, int(_as_float(data.get("hits"), 1))),
            slot=str(data.get("slot", "") or infer_slot(text)),
        )


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class UserState:
    """单个私聊用户的完整状态。"""

    user_id: str
    first_seen: float = 0.0
    last_seen: float = 0.0
    interactions: int = 0
    opt_out: bool = False
    last_sent: float = 0.0
    sent_date: str = ""
    sent_count: int = 0
    turns: list[Turn] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    summary: str = ""
    pending_turns: int = 0
    emotion: EmotionState = field(default_factory=EmotionState)
    failure_count: int = 0
    retry_after: float = 0.0
    last_error: str = ""
    active_dates: list[str] = field(default_factory=list)

    def mark_active(self, now: float) -> None:
        """记录一个活跃日期（用于按活跃天数判断关系阶段）。"""
        day = (
            datetime.fromtimestamp(now, tz=timezone.utc).astimezone().date().isoformat()
        )
        if day in self.active_dates:
            return
        self.active_dates.append(day)
        self.active_dates.sort()
        if len(self.active_dates) > _MAX_ACTIVE_DATES:
            del self.active_dates[:-_MAX_ACTIVE_DATES]

    def prune_memory(self, now: float, retention_days: int) -> int:
        """清理超过保留期的记忆与对话，返回删除条数。"""
        if retention_days <= 0:
            return 0
        cutoff = now - retention_days * 86400
        before = len(self.facts) + len(self.turns)
        self.facts = [fact for fact in self.facts if fact.ts >= cutoff]
        self.turns = [turn for turn in self.turns if turn.ts >= cutoff]
        removed = before - len(self.facts) - len(self.turns)
        if removed and self.summary:
            # 概述可能包含已过期内容，清理后重新生成
            self.summary = ""
        cutoff_day = (
            datetime.fromtimestamp(cutoff, tz=timezone.utc)
            .astimezone()
            .date()
            .isoformat()
        )
        self.active_dates = [day for day in self.active_dates if day >= cutoff_day]
        return removed

    def export_memory(self) -> dict[str, Any]:
        """导出当前用户的记忆快照，供 /dsh 导出 使用。"""
        return {
            "user_id": self.user_id,
            "summary": self.summary,
            "facts": [fact.text for fact in self.facts],
            "turns": [
                {"role": turn.role, "text": turn.text} for turn in self.turns[-10:]
            ],
            "emotion": self.emotion.to_dict(),
            "interactions": self.interactions,
            "active_days": len(self.active_dates),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "interactions": self.interactions,
            "opt_out": self.opt_out,
            "last_sent": self.last_sent,
            "sent_date": self.sent_date,
            "sent_count": self.sent_count,
            "turns": [turn.to_dict() for turn in self.turns],
            "facts": [fact.to_dict() for fact in self.facts],
            "summary": self.summary,
            "pending_turns": self.pending_turns,
            "emotion": self.emotion.to_dict(),
            "failure_count": self.failure_count,
            "retry_after": self.retry_after,
            "last_error": self.last_error,
            "active_dates": list(self.active_dates),
        }

    @classmethod
    def from_dict(cls, user_id: str, data: Any) -> UserState:
        if not isinstance(data, dict):
            return cls(user_id=user_id)
        turns: list[Turn] = []
        raw_turns = data.get("turns")
        if isinstance(raw_turns, list):
            turns = [
                turn for turn in map(Turn.from_dict, raw_turns) if turn is not None
            ]
        facts: list[Fact] = []
        raw_facts = data.get("facts")
        if isinstance(raw_facts, list):
            facts = [
                fact for fact in map(Fact.from_dict, raw_facts) if fact is not None
            ]
        raw_dates = data.get("active_dates")
        active_dates = (
            sorted({str(day) for day in raw_dates if str(day)})[-_MAX_ACTIVE_DATES:]
            if isinstance(raw_dates, list)
            else []
        )
        return cls(
            user_id=user_id,
            first_seen=max(0.0, _as_float(data.get("first_seen"))),
            last_seen=max(0.0, _as_float(data.get("last_seen"))),
            interactions=max(0, int(_as_float(data.get("interactions")))),
            opt_out=bool(data.get("opt_out", False)),
            last_sent=max(0.0, _as_float(data.get("last_sent"))),
            sent_date=str(data.get("sent_date", "")),
            sent_count=max(0, int(_as_float(data.get("sent_count")))),
            turns=turns,
            facts=facts,
            summary=str(data.get("summary", ""))[:400],
            pending_turns=max(0, int(_as_float(data.get("pending_turns")))),
            emotion=EmotionState.from_dict(data.get("emotion")),
            failure_count=max(0, int(_as_float(data.get("failure_count")))),
            retry_after=max(0.0, _as_float(data.get("retry_after"))),
            last_error=str(data.get("last_error", ""))[:200],
            active_dates=active_dates,
        )


class StateStore:
    """state.json 的读写与内存索引。"""

    def __init__(self, path: Path, *, max_users: int = _MAX_USERS) -> None:
        self._path = Path(path)
        self._max_users = max(1, max_users)
        self._users: dict[str, UserState] = {}
        self._save_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def __len__(self) -> int:
        return len(self._users)

    def load(self) -> None:
        """加载状态文件；不存在时保持空状态，损坏时备份并重新开始。"""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("陪伴插件状态文件读取失败，使用空状态", exc_info=True)
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise TypeError("state.json 顶层必须为对象")
        except (TypeError, ValueError):
            logger.warning("陪伴插件状态文件损坏，已备份并重置", exc_info=True)
            self._backup_corrupt()
            return
        self._users = self._parse(data)

    def _backup_corrupt(self) -> None:
        try:
            self._path.replace(self._path.with_suffix(".corrupt.json"))
        except OSError:
            logger.debug("备份损坏的状态文件失败", exc_info=True)

    def _parse(self, data: dict[str, Any]) -> dict[str, UserState]:
        users: dict[str, UserState] = {}
        raw_users = data.get("users")
        if isinstance(raw_users, dict):
            for user_id, payload in raw_users.items():
                key = str(user_id).strip()
                if key:
                    users[key] = UserState.from_dict(key, payload)
            return users
        # v1 迁移：顶层扁平结构，仅保留私聊记录
        migrated: dict[str, UserState] = {}
        last_sent = data.get("last_sent")
        if isinstance(last_sent, dict):
            for raw_key, value in last_sent.items():
                key = str(raw_key)
                if not key.startswith(_PRIVATE_PREFIX):
                    continue
                user_id = key[len(_PRIVATE_PREFIX) :]
                if user_id:
                    migrated.setdefault(
                        user_id, UserState(user_id=user_id)
                    ).last_sent = max(0.0, _as_float(value))
        sent_today = data.get("sent_today")
        if isinstance(sent_today, dict):
            for raw_key, value in sent_today.items():
                key = str(raw_key)
                if not key.startswith(_PRIVATE_PREFIX) or not isinstance(value, dict):
                    continue
                user_id = key[len(_PRIVATE_PREFIX) :]
                if not user_id:
                    continue
                state = migrated.setdefault(user_id, UserState(user_id=user_id))
                state.sent_date = str(value.get("date", ""))
                state.sent_count = max(0, int(_as_float(value.get("count"))))
        opt_out = data.get("opt_out")
        if isinstance(opt_out, list):
            for raw_key in opt_out:
                key = str(raw_key)
                if not key.startswith(_PRIVATE_PREFIX):
                    continue
                user_id = key[len(_PRIVATE_PREFIX) :]
                if user_id:
                    migrated.setdefault(
                        user_id, UserState(user_id=user_id)
                    ).opt_out = True
        return migrated

    def get(self, user_id: str, now: float | None = None) -> UserState:
        """获取用户状态，不存在时创建。"""
        key = str(user_id)
        state = self._users.get(key)
        if state is None:
            timestamp = time.time() if now is None else now
            state = UserState(user_id=key, first_seen=timestamp, last_seen=timestamp)
            self._users[key] = state
        return state

    def peek(self, user_id: str) -> UserState | None:
        return self._users.get(str(user_id))

    def items(self) -> list[UserState]:
        return list(self._users.values())

    def prune(self) -> None:
        """超出用户上限时按最近活跃时间淘汰，防止状态文件无限增长。"""
        if len(self._users) <= self._max_users:
            return
        ordered = sorted(
            self._users.values(), key=lambda item: item.last_seen, reverse=True
        )
        keep = {item.user_id for item in ordered[: self._max_users]}
        self._users = {
            user_id: state for user_id, state in self._users.items() if user_id in keep
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "users": {
                user_id: state.to_dict() for user_id, state in self._users.items()
            },
        }

    def save(self) -> None:
        """同步写入（测试或退出时使用）。"""
        self.prune()
        self._write_snapshot(self.snapshot())

    def _write_snapshot(self, snapshot: dict[str, Any]) -> None:
        """只做磁盘写入；快照在事件循环线程生成，避免遍历时被并发修改。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._path)

    async def save_async(self) -> None:
        """异步保存：在事件循环内取快照，写入放到线程，且串行化。"""
        async with self._save_lock:
            self.prune()
            snapshot = self.snapshot()
            await asyncio.to_thread(self._write_snapshot, snapshot)
