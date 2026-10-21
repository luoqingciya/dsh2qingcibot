"""情绪模型：鲸鱼娘自身的情绪状态与对用户情绪的轻量感知。

设计原则：

- 情绪只用于调整语气与关注点，不伪装成人类情感，也不夸大共情；
- 状态随时间回归基线，避免一次负面互动长期影响后续回复；
- 用户情绪判断使用本地词表，不额外调用模型，避免成本与延迟；
- 词表命中会检查前置否定词与程度副词（「不开心」「不太累」），
  避免把否定表达判成相反极性；
- 识别到高风险表达时，提示词转入支持性回应，并建议寻求现实帮助。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# 情绪基线：略微正向、偏低唤醒、中等精力
BASELINE_VALENCE = 0.12
BASELINE_AROUSAL = 0.35
BASELINE_ENERGY = 0.62

_POSITIVE_WORDS = (
    "开心",
    "高兴",
    "愉快",
    "舒服",
    "顺利",
    "成功",
    "太好了",
    "谢谢",
    "喜欢",
    "期待",
    "好耶",
    "哈哈",
    "嘿嘿",
    "棒",
    "可爱",
    "放松",
    "安心",
)
_NEGATIVE_WORDS = (
    "难过",
    "累",
    "疲惫",
    "烦",
    "焦虑",
    "压力",
    "失眠",
    "孤独",
    "委屈",
    "生气",
    "崩溃",
    "难受",
    "想哭",
    "失败",
    "害怕",
    "担心",
    "无聊",
)
# 高风险表达：直接命中或间接表达都进入支持性回应
_DISTRESS_WORDS = (
    "想死",
    "不想活",
    "活不下去",
    "结束生命",
    "自残",
    "撑不住",
    "活着没意思",
    "不想醒来",
    "消失就好了",
    "一了百了",
    "没有我会更好",
)
_TIRED_WORDS = ("累", "困", "熬夜", "没睡", "加班", "通宵")
_ENERGETIC_WORDS = ("睡醒", "精神", "有劲", "兴奋", "刚运动")

_NEGATION_MARKERS = ("不", "没", "别", "无", "未", "非")
# 「特别」「非常」「无比」等词含有否定字，但整体不是否定
_NOT_NEGATION_SUFFIXES = (
    "特别",
    "分别",
    "区别",
    "个别",
    "告别",
    "非常",
    "无比",
    "非但",
)
_DEGREE_HIGH = ("很", "特别", "超", "太", "非常", "巨")
_DEGREE_LOW = ("有点", "稍微", "一点", "有些")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _hits(text: str, word: str) -> list[int]:
    """返回 word 在 text 中的所有起始下标。"""
    indexes: list[int] = []
    start = 0
    while True:
        index = text.find(word, start)
        if index < 0:
            return indexes
        indexes.append(index)
        start = index + len(word)


def _is_negated(text: str, index: int, window: int = 3) -> bool:
    """命中词前 window 个字符内出现否定词即视为否定。"""
    prefix = text[max(0, index - window) : index]
    if prefix.endswith(_NOT_NEGATION_SUFFIXES):
        return False
    return any(marker in prefix for marker in _NEGATION_MARKERS)


def _degree(text: str, index: int) -> float:
    """程度副词权重：很/特别 → 1.4，有点/稍微 → 0.7。"""
    prefix = text[max(0, index - 3) : index]
    if any(word in prefix for word in _DEGREE_HIGH):
        return 1.4
    if any(word in prefix for word in _DEGREE_LOW):
        return 0.7
    return 1.0


def _weighted_hits(text: str) -> tuple[float, float]:
    """返回 (正向权重, 负向权重)；否定表达按相反极性计入。"""
    positive = 0.0
    negative = 0.0
    for word in _POSITIVE_WORDS:
        for index in _hits(text, word):
            weight = _degree(text, index)
            if _is_negated(text, index):
                negative += weight
            else:
                positive += weight
    for word in _NEGATIVE_WORDS:
        for index in _hits(text, word):
            weight = _degree(text, index)
            if _is_negated(text, index):
                positive += weight * 0.8
            else:
                negative += weight
    return positive, negative


def _mood_scores(text: str) -> tuple[float, float, float]:
    """返回 (正向, 负向, 疲惫)；否定正向词按负向计，否定负向词视为中性。"""
    positive = 0.0
    negative = 0.0
    for word in _POSITIVE_WORDS:
        for index in _hits(text, word):
            weight = _degree(text, index)
            if _is_negated(text, index):
                negative += weight
            else:
                positive += weight
    for word in _NEGATIVE_WORDS:
        for index in _hits(text, word):
            if not _is_negated(text, index):
                negative += _degree(text, index)
    tired = 0.0
    for word in _TIRED_WORDS:
        for index in _hits(text, word):
            if not _is_negated(text, index):
                tired += _degree(text, index)
    return positive, negative, tired


@dataclass
class EmotionState:
    """三维情绪状态，取值均在 [0, 1] 或 [-1, 1] 区间内。"""

    valence: float = BASELINE_VALENCE  # 愉悦度：-1 低落 … 1 愉快
    arousal: float = BASELINE_AROUSAL  # 唤醒度：0 安静 … 1 兴奋
    energy: float = BASELINE_ENERGY  # 精力：0 疲惫 … 1 充沛
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "valence": round(self.valence, 4),
            "arousal": round(self.arousal, 4),
            "energy": round(self.energy, 4),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> EmotionState:
        if not isinstance(data, dict):
            return cls()
        return cls(
            valence=_clamp(_as_float(data.get("valence"), BASELINE_VALENCE), -1.0, 1.0),
            arousal=_clamp(_as_float(data.get("arousal"), BASELINE_AROUSAL), 0.0, 1.0),
            energy=_clamp(_as_float(data.get("energy"), BASELINE_ENERGY), 0.0, 1.0),
            updated_at=max(0.0, _as_float(data.get("updated_at"), 0.0)),
        )


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def decay(state: EmotionState, now: float, half_life_hours: float) -> EmotionState:
    """按半衰期把情绪拉回基线；未记录时间戳时直接返回基线。"""
    if state.updated_at <= 0:
        return EmotionState(updated_at=now)
    half_life = max(0.5, float(half_life_hours))
    elapsed_hours = max(0.0, now - state.updated_at) / 3600.0
    factor = math.pow(0.5, elapsed_hours / half_life)
    return EmotionState(
        valence=BASELINE_VALENCE + (state.valence - BASELINE_VALENCE) * factor,
        arousal=BASELINE_AROUSAL + (state.arousal - BASELINE_AROUSAL) * factor,
        energy=BASELINE_ENERGY + (state.energy - BASELINE_ENERGY) * factor,
        updated_at=now,
    )


def observe(text: str) -> tuple[float, float, float]:
    """从一条用户消息估计情绪增量 (valence, arousal, energy)。"""
    content = text or ""
    positive, negative = _weighted_hits(content)
    _, _, tired = _mood_scores(content)
    energetic = 0.0
    for word in _ENERGETIC_WORDS:
        for index in _hits(content, word):
            if not _is_negated(content, index):
                energetic += _degree(content, index)
    exclamations = content.count("!") + content.count("！")

    valence = _clamp(0.14 * positive - 0.18 * negative, -0.5, 0.5)
    arousal = _clamp(0.04 * positive + 0.04 * negative + 0.03 * exclamations, -0.3, 0.3)
    energy = _clamp(0.05 * energetic - 0.07 * tired, -0.3, 0.2)
    if is_distress(content):
        valence -= 0.25
        energy -= 0.15
    return valence, arousal, energy


def apply_observation(
    state: EmotionState, text: str, now: float, half_life_hours: float
) -> EmotionState:
    """先衰减到当前时刻，再叠加本次消息带来的情绪变化。"""
    current = decay(state, now, half_life_hours)
    d_valence, d_arousal, d_energy = observe(text)
    return EmotionState(
        valence=_clamp(current.valence + d_valence, -1.0, 1.0),
        arousal=_clamp(current.arousal + d_arousal, 0.0, 1.0),
        energy=_clamp(current.energy + d_energy, 0.0, 1.0),
        updated_at=now,
    )


def label(state: EmotionState) -> str:
    """把情绪状态映射为中文标签，用于提示词与状态展示。"""
    if state.valence >= 0.45 and state.arousal >= 0.55:
        return "雀跃"
    if state.valence >= 0.25:
        return "愉快"
    if state.valence <= -0.35:
        return "低落"
    if state.valence <= -0.15:
        return "有点担心"
    if state.arousal <= 0.25:
        return "安静"
    if state.energy <= 0.3:
        return "有点累"
    return "平静"


def tone_hint(state: EmotionState) -> str:
    """情绪对应的语气提示，直接进入系统提示词。"""
    hints = {
        "雀跃": "语气轻快，可以有一点俏皮的海洋比喻，但不要连续使用感叹号。",
        "愉快": "语气温和自然，允许少量轻松玩笑。",
        "低落": "语气放轻，少用感叹号和卖萌，先照顾对方，不强行振作。",
        "有点担心": "语气温柔克制，先确认对方的状态，再决定是否给建议。",
        "安静": "语气平静简洁，不刻意活跃气氛。",
        "有点累": "句子短一些，回应要实在，不铺陈。",
        "平静": "语气平和，以倾听和回应为主。",
    }
    return hints.get(label(state), hints["平静"])


def is_distress(text: str) -> bool:
    """是否出现需要认真对待的高风险表达（排除「不想死」这类否定）。"""
    content = text or ""
    for word in _DISTRESS_WORDS:
        for index in _hits(content, word):
            if not _is_negated(content, index, window=2):
                return True
    return False


def user_mood(text: str) -> str:
    """粗分用户当前状态，用于选择回应策略。"""
    content = text or ""
    if is_distress(content):
        return "需要支持"
    positive, negative, tired = _mood_scores(content)
    if tired >= 1 and negative <= 1 and positive == 0:
        return "疲惫"
    if negative >= 2 or (negative >= 1 and positive == 0):
        return "低落"
    if positive > negative:
        return "愉快"
    return "平静"


def mood_guidance(mood: str) -> str:
    """不同用户状态下的回应要求。"""
    guidance = {
        "需要支持": (
            "对方可能正处于很艰难的处境。先表达在意并认真听，不评判、不说教，"
            "不提供任何伤害自己的方法；鼓励对方联系信任的人或当地专业心理援助，"
            "并明确你只是虚拟陪伴、不能替代现实支持。"
        ),
        "低落": (
            "对方情绪偏低。先接住感受再谈其他，不急着给解决方案，"
            "不强行乐观，也不要追问过多细节。"
        ),
        "疲惫": "对方可能很累。回应简短、务实，减少提问，允许对方暂时不回复。",
        "愉快": "对方心情不错。可以顺着话题分享一点轻松的感受，但不要抢话。",
        "平静": "按对方给出的话题自然回应，一次只推进一个点。",
    }
    return guidance.get(mood, guidance["平静"])
