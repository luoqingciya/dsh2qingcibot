from __future__ import annotations

import json

import pytest
from dsh2qingcibot.emotion import EmotionState
from dsh2qingcibot.state import Fact, StateStore, Turn, UserState


def test_get_creates_profile_with_timestamps(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")

    profile = store.get("123", now=100.0)

    assert profile.user_id == "123"
    assert profile.first_seen == 100.0
    assert profile.last_seen == 100.0
    assert store.peek("123") is profile


def test_v1_state_is_migrated_and_group_entries_dropped(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "last_sent": {"private:123": 10, "group:456": 20},
                "sent_today": {
                    "private:123": {"date": "2026-10-20", "count": 1},
                    "group:456": {"date": "2026-10-20", "count": 2},
                },
                "opt_out": ["private:123", "group:456"],
            }
        ),
        encoding="utf-8",
    )
    store = StateStore(path)

    store.load()

    profile = store.peek("123")
    assert profile is not None
    assert profile.last_sent == 10.0
    assert profile.sent_date == "2026-10-20"
    assert profile.sent_count == 1
    assert profile.opt_out is True
    assert store.peek("456") is None


def test_save_and_load_roundtrip(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    profile = store.get("123", now=100.0)
    profile.interactions = 3
    profile.summary = "用户在准备考试"
    profile.turns = [Turn(role="user", text="我要考试了", ts=120.0)]
    profile.facts = [Fact(text="计划 准备考试", ts=120.0, hits=2)]
    profile.emotion = EmotionState(
        valence=0.4, arousal=0.5, energy=0.5, updated_at=120.0
    )

    store.save()

    reloaded = StateStore(path)
    reloaded.load()
    restored = reloaded.get("123")
    assert restored.interactions == 3
    assert restored.summary == "用户在准备考试"
    assert restored.turns[0].text == "我要考试了"
    assert restored.facts[0].hits == 2
    assert restored.emotion.valence == 0.4
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2


def test_corrupt_state_is_backed_up(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")
    store = StateStore(path)

    store.load()

    assert len(store) == 0
    assert (tmp_path / "state.corrupt.json").is_file()


def test_prune_keeps_most_recent_users(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path, max_users=2)
    for user_id, seen in (("a", 1.0), ("b", 2.0), ("c", 3.0)):
        profile = store.get(user_id, now=seen)
        profile.last_seen = seen

    store.save()

    reloaded = StateStore(path, max_users=2)
    reloaded.load()
    assert {profile.user_id for profile in reloaded.items()} == {"b", "c"}


def test_failure_fields_roundtrip(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    profile = store.get("123", now=100.0)
    profile.failure_count = 2
    profile.retry_after = 500.0
    profile.last_error = "RuntimeError: boom"

    store.save()

    reloaded = StateStore(path)
    reloaded.load()
    restored = reloaded.get("123")
    assert restored.failure_count == 2
    assert restored.retry_after == 500.0
    assert restored.last_error == "RuntimeError: boom"


def test_mark_active_deduplicates_days(tmp_path) -> None:
    profile = StateStore(tmp_path / "state.json").get("1", now=100.0)

    profile.mark_active(100.0)
    profile.mark_active(200.0)
    profile.mark_active(300.0)

    assert len(profile.active_dates) == 1


def test_prune_memory_respects_retention(tmp_path) -> None:
    now = 1_700_000_000.0
    old = now - 40 * 86400
    profile = StateStore(tmp_path / "state.json").get("1", now=now)
    profile.facts = [Fact("旧记忆", old), Fact("新记忆", now)]
    profile.turns = [Turn("user", "旧对话", old), Turn("user", "新对话", now)]
    profile.summary = "旧概述"
    profile.active_dates = ["2000-01-01"]

    removed = profile.prune_memory(now, retention_days=30)

    assert removed == 2
    assert [fact.text for fact in profile.facts] == ["新记忆"]
    assert [turn.text for turn in profile.turns] == ["新对话"]
    assert profile.summary == ""
    assert profile.active_dates == []


def test_export_memory_contains_snapshot(tmp_path) -> None:
    profile = StateStore(tmp_path / "state.json").get("1", now=100.0)
    profile.summary = "用户在准备考试"
    profile.facts = [Fact("计划 下周三考试", 100.0)]
    profile.interactions = 3

    exported = profile.export_memory()

    assert exported["summary"] == "用户在准备考试"
    assert exported["facts"] == ["计划 下周三考试"]
    assert exported["interactions"] == 3


@pytest.mark.asyncio
async def test_save_async_writes_snapshot(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.get("1", now=100.0)

    await store.save_async()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["users"]["1"]["first_seen"] == 100.0


def test_user_state_tolerates_bad_payload() -> None:
    profile = UserState.from_dict(
        "1",
        {
            "turns": ["bad", {"role": "user", "text": "hi", "ts": "x"}],
            "facts": [{"text": ""}, {"text": "喜欢 猫"}],
            "emotion": {"valence": "oops"},
            "interactions": "7",
        },
    )

    assert [turn.text for turn in profile.turns] == ["hi"]
    assert [fact.text for fact in profile.facts] == ["喜欢 猫"]
    assert profile.interactions == 7
    assert profile.emotion.updated_at == 0.0
