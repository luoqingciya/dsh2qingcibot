from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from dsh2qingcibot.diary import DiaryEntry, DiaryStore, write_entry
from dsh2qingcibot.state import Fact, StateStore, Turn

NOW = 1_700_000_000.0
MOMENT = datetime.fromtimestamp(NOW).astimezone()


def make_profile(tmp_path, *, turns: bool = True):
    store = StateStore(tmp_path / "state.json")
    profile = store.get("123", now=NOW)
    profile.summary = "用户在准备考试"
    profile.facts = [Fact("计划 下周三考试", NOW)]
    if turns:
        profile.turns = [
            Turn("user", "我下周三要考试，有点紧张", NOW - 60),
            Turn("assistant", "那我陪你慢慢复习", NOW - 30),
        ]
    return profile


def test_append_and_read_entries(tmp_path) -> None:
    store = DiaryStore(tmp_path / "diary.json")
    entry = DiaryEntry(ts=NOW, text="今天聊了考试。", mood="平静")

    store.append("123", entry, limit=3)

    assert store.latest("123") is entry
    assert store.latest_text("123") == "今天聊了考试。"
    day = datetime.fromtimestamp(NOW, tz=timezone.utc).astimezone().date().isoformat()
    assert store.has_entry_on("123", day) is True
    assert store.entries("123", limit=1) == [entry]


def test_save_and_load_roundtrip(tmp_path) -> None:
    path = tmp_path / "diary.json"
    store = DiaryStore(path)
    store.append("123", DiaryEntry(ts=NOW, text="第一篇", mood="愉快"), limit=5)
    store.append("123", DiaryEntry(ts=NOW + 86400, text="第二篇", mood="平静"), limit=5)

    store.save()

    reloaded = DiaryStore(path)
    reloaded.load()
    assert [entry.text for entry in reloaded.entries("123")] == ["第一篇", "第二篇"]
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_append_respects_entry_limit(tmp_path) -> None:
    store = DiaryStore(tmp_path / "diary.json")
    for index in range(5):
        store.append(
            "123", DiaryEntry(ts=NOW + index, text=f"第{index}篇", mood="平静"), limit=3
        )

    assert [entry.text for entry in store.entries("123")] == ["第2篇", "第3篇", "第4篇"]


def test_clear_removes_all_entries(tmp_path) -> None:
    store = DiaryStore(tmp_path / "diary.json")
    store.append("123", DiaryEntry(ts=NOW, text="一篇", mood="平静"), limit=5)

    assert store.clear("123") == 1
    assert store.entries("123") == []


def test_prune_removes_expired_entries(tmp_path) -> None:
    store = DiaryStore(tmp_path / "diary.json")
    old = NOW - 40 * 86400
    store.append("123", DiaryEntry(ts=old, text="旧日记", mood="平静"), limit=30)
    store.append("123", DiaryEntry(ts=NOW, text="新日记", mood="平静"), limit=30)

    removed = store.prune(NOW, retention_days=30, limit=30)

    assert removed == 1
    assert [entry.text for entry in store.entries("123")] == ["新日记"]


class _Result:
    content = "今天和对方聊了考试，心里有点紧，但也替他期待明天。"


class _Adapter:
    def __init__(self) -> None:
        self.prompt = ""

    async def chat_detail(self, **kwargs):
        self.prompt = kwargs["messages"][0]["content"]
        return _Result()


class _LLM:
    def __init__(self) -> None:
        self.adapter = _Adapter()


@pytest.mark.asyncio
async def test_write_entry_uses_dialogue_and_memory(tmp_path) -> None:
    profile = make_profile(tmp_path)
    llm = _LLM()

    entry = await write_entry(llm, profile, NOW, moment=MOMENT, half_life_hours=12)

    assert entry is not None
    assert "考试" in entry.text
    assert entry.mood
    assert "我下周三要考试" in llm.adapter.prompt
    assert "计划 下周三考试" in llm.adapter.prompt
    assert "只输出日记正文" in llm.adapter.prompt


@pytest.mark.asyncio
async def test_write_entry_without_adapter_returns_none(tmp_path) -> None:
    profile = make_profile(tmp_path)

    assert (
        await write_entry(object(), profile, NOW, moment=MOMENT, half_life_hours=12)
        is None
    )


@pytest.mark.asyncio
async def test_write_entry_handles_empty_output(tmp_path) -> None:
    class EmptyAdapter:
        async def chat_detail(self, **kwargs):
            return type("R", (), {"content": "   "})()

    profile = make_profile(tmp_path)
    llm = type("L", (), {"adapter": EmptyAdapter()})()

    assert (
        await write_entry(llm, profile, NOW, moment=MOMENT, half_life_hours=12) is None
    )
