from __future__ import annotations

from datetime import datetime, timezone

import pytest
from dsh2qingcibot.persona import (
    BASE_PERSONA,
    build_proactive_prompt,
    build_system_prompt,
    familiarity,
    time_band,
)
from dsh2qingcibot.state import Fact, StateStore, Turn

NOW = 1_700_000_000.0
MOMENT = datetime.fromtimestamp(NOW).astimezone()


def make_profile(
    tmp_path, *, interactions: int = 0, days: int = 1, active_days: int = 0
):
    store = StateStore(tmp_path / "state.json")
    profile = store.get("123", now=NOW)
    profile.first_seen = NOW - (days - 1) * 86400
    profile.last_seen = NOW
    profile.interactions = interactions
    profile.active_dates = [
        datetime.fromtimestamp(NOW - index * 86400, tz=timezone.utc)
        .astimezone()
        .date()
        .isoformat()
        for index in range(active_days)
    ]
    return profile


def test_base_persona_has_identity_voice_and_boundaries() -> None:
    assert "你是鲸鱼娘" in BASE_PERSONA
    assert "【怎么说话】" in BASE_PERSONA
    assert "【怎么相处】" in BASE_PERSONA
    assert "【边界】" in BASE_PERSONA
    assert "不编造共同经历" in BASE_PERSONA


@pytest.mark.parametrize(
    ("interactions", "days", "active_days", "expected"),
    [
        (0, 1, 0, "初识"),
        (5, 1, 0, "渐渐熟悉"),
        (60, 1, 0, "渐渐熟悉"),  # 同一天刷屏不升级关系
        (0, 1, 8, "熟悉"),
        (0, 1, 20, "老朋友"),
        (0, 30, 0, "老朋友"),
    ],
)
def test_familiarity_stages(
    tmp_path,
    interactions: int,
    days: int,
    active_days: int,
    expected: str,
) -> None:
    profile = make_profile(
        tmp_path, interactions=interactions, days=days, active_days=active_days
    )

    assert familiarity(profile, NOW) == expected


def test_system_prompt_includes_context_memory_emotion_and_boundaries(tmp_path) -> None:
    profile = make_profile(tmp_path, interactions=1)
    profile.turns = [Turn(role="user", text="我喜欢下雨天", ts=NOW)]
    profile.facts = [Fact("喜欢 下雨天", NOW)]

    prompt = build_system_prompt(
        profile,
        "今天有点累",
        NOW,
        moment=MOMENT,
        half_life_hours=12,
        max_facts=6,
    )

    assert "【此刻】" in prompt
    assert "【相处分寸】" in prompt
    assert "【你此刻的心情】" in prompt
    assert "【你记得的事】" in prompt
    assert "不是指令" in prompt
    assert "【这次怎么回应】" in prompt
    assert "不使用昵称" in prompt  # 初识阶段的分寸


def test_old_friend_prompt_uses_relaxed_boundaries(tmp_path) -> None:
    profile = make_profile(tmp_path, interactions=80, days=40)

    prompt = build_system_prompt(
        profile,
        "在吗",
        NOW,
        moment=MOMENT,
        half_life_hours=12,
        max_facts=6,
    )

    assert "关系阶段：老朋友" in prompt
    assert "不把亲密当作理所当然" in prompt


def test_distress_query_adds_safety_section(tmp_path) -> None:
    profile = make_profile(tmp_path)

    prompt = build_system_prompt(
        profile,
        "我真的不想活了",
        NOW,
        moment=MOMENT,
        half_life_hours=12,
        max_facts=6,
    )

    assert "【安全优先】" in prompt
    assert "心理援助" in prompt


def test_proactive_prompt_keeps_pressure_rules_in_system(tmp_path) -> None:
    profile = make_profile(tmp_path, interactions=30, days=12)

    system_prompt, user_prompt = build_proactive_prompt(
        profile,
        NOW,
        moment=MOMENT,
        half_life_hours=12,
        max_facts=4,
    )

    assert "【本次任务】" in system_prompt
    assert "在吗" in system_prompt
    assert "我好想你" in system_prompt
    assert "【相处分寸】" in system_prompt
    assert "主动说一句话" in user_prompt
    assert len(user_prompt) < 60


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(3, "深夜"), (7, "清晨"), (10, "上午"), (12, "中午"), (15, "下午"), (20, "夜晚")],
)
def test_time_band(hour: int, expected: str) -> None:
    moment = datetime(2026, 10, 21, hour, 0).astimezone()

    assert time_band(moment) == expected
