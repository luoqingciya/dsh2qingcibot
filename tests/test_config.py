from __future__ import annotations

import pytest
from dsh2qingcibot.config import CompanionConfig


def test_defaults_are_conservative() -> None:
    config = CompanionConfig()

    assert config.enabled is True
    assert config.private_targets == []
    assert config.private_auto_reply is True
    assert config.persona_name == "鲸鱼娘"
    assert config.interval_hours == 8
    assert config.idle_minutes == 60
    assert config.daily_limit == 2
    assert config.memory_turns == 12
    assert config.memory_facts == 24
    assert config.memory_summary_every == 4
    assert config.memory_retention_days == 90
    assert config.diary_enabled is True
    assert config.diary_hour == 23
    assert config.diary_max_entries == 30


def test_unknown_legacy_fields_are_ignored() -> None:
    config = CompanionConfig(group_targets=["987654321"], unknown_field=1)

    assert config.private_targets == []
    assert "group_targets" not in config.model_dump()


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"interval_hours": 0}, ValueError),
        ({"daily_limit": 6}, ValueError),
        ({"memory_turns": 1}, ValueError),
        ({"memory_facts": 101}, ValueError),
        ({"emotion_decay_hours": 0}, ValueError),
        ({"memory_retention_days": -1}, ValueError),
        ({"diary_hour": 24}, ValueError),
        ({"diary_max_entries": 4}, ValueError),
        ({"private_targets": "123"}, TypeError),
        ({"enabled": "yes"}, TypeError),
        ({"persona_name": 123}, TypeError),
    ],
)
def test_invalid_values_are_rejected(kwargs: dict, error: type[Exception]) -> None:
    with pytest.raises(error):
        CompanionConfig(**kwargs)


def test_targets_are_trimmed_and_deduplicated_by_plugin() -> None:
    config = CompanionConfig(private_targets=[" 123 ", "123", ""])

    assert config.private_targets == ["123", "123"]


def test_schema_covers_memory_and_emotion_fields() -> None:
    properties = CompanionConfig.model_json_schema()["properties"]

    assert {"memory_turns", "memory_facts", "memory_summary_every"} <= set(properties)
    assert "emotion_decay_hours" in properties
    assert "memory_retention_days" in properties
    assert {"diary_enabled", "diary_hour", "diary_max_entries"} <= set(properties)
    assert "private_auto_reply" in properties
    assert "persona_name" in properties
    assert "group_targets" not in properties
    assert CompanionConfig.model_dump(CompanionConfig())["memory_facts"] == 24
