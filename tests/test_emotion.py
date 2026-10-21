from __future__ import annotations

import pytest
from dsh2qingcibot.emotion import (
    BASELINE_VALENCE,
    EmotionState,
    apply_observation,
    decay,
    is_distress,
    label,
    observe,
    tone_hint,
    user_mood,
)


def test_decay_returns_to_baseline_over_time() -> None:
    state = EmotionState(valence=0.9, arousal=0.9, energy=0.1, updated_at=0)

    after_long = decay(state, now=10 * 86400, half_life_hours=12)

    assert after_long.valence == pytest.approx(BASELINE_VALENCE, abs=0.01)
    assert after_long.arousal == pytest.approx(0.35, abs=0.01)
    assert after_long.energy == pytest.approx(0.62, abs=0.01)


def test_positive_message_lifts_mood() -> None:
    state = EmotionState(updated_at=1000)

    updated = apply_observation(state, "今天太开心了，事情很顺利，太好了！", 1000, 12)

    assert updated.valence > state.valence
    assert updated.updated_at == 1000


def test_negative_message_lowers_mood_and_energy() -> None:
    state = EmotionState(updated_at=1000)

    updated = apply_observation(state, "最近好累，压力很大，有点难过", 1000, 12)

    assert updated.valence < state.valence
    assert updated.energy < state.energy


def test_distress_lowers_valence_further() -> None:
    normal = observe("我有点难过")
    distress = observe("我真的不想活了")

    assert is_distress("我真的不想活了") is True
    assert distress[0] < normal[0]


def test_negation_flips_sentiment() -> None:
    assert observe("不开心")[0] < 0
    assert observe("我不累")[0] >= 0
    assert user_mood("不开心") == "低落"
    assert user_mood("我不累") != "疲惫"


def test_degree_words_with_negation_chars_are_not_negated() -> None:
    assert observe("非常开心")[0] > 0
    assert observe("无比开心")[0] > 0
    assert user_mood("非常开心") == "愉快"


def test_degree_modifier_scales_intensity() -> None:
    mild = observe("有点难过")[0]
    strong = observe("特别难过")[0]

    assert strong < mild < 0


def test_indirect_distress_is_detected_but_negated_is_not() -> None:
    assert is_distress("不想醒来") is True
    assert is_distress("我不想死") is False


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (EmotionState(valence=0.6, arousal=0.8), "雀跃"),
        (EmotionState(valence=0.3, arousal=0.4), "愉快"),
        (EmotionState(valence=-0.5, arousal=0.4), "低落"),
        (EmotionState(valence=-0.2, arousal=0.4), "有点担心"),
        (EmotionState(valence=0.1, arousal=0.1), "安静"),
        (EmotionState(valence=0.1, arousal=0.5, energy=0.2), "有点累"),
        (EmotionState(valence=0.1, arousal=0.5, energy=0.7), "平静"),
    ],
)
def test_label_mapping(state: EmotionState, expected: str) -> None:
    assert label(state) == expected
    assert tone_hint(state)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("今天很开心，谢谢你", "愉快"),
        ("好累，什么都不想做", "疲惫"),
        ("有点难过，也有点焦虑", "低落"),
        ("吃过了", "平静"),
        ("撑不住了", "需要支持"),
    ],
)
def test_user_mood(text: str, expected: str) -> None:
    assert user_mood(text) == expected
