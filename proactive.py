"""定时任务侧：记忆整理与主动消息发送。

与插件类解耦，便于在不启动 Qingci-Bot 的情况下用假连接测试发送策略。
发送失败会按次数退避，连续失败进入较长冷却，避免每个 tick 重复撞同一错误。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from .companion import CompanionEngine
from .config import CompanionConfig
from .memory import overlap, tokenize
from .state import UserState

logger = logging.getLogger("qingci-bot.plugin.whale_companion.proactive")

Persist = Callable[[], Awaitable[None]]
DiaryProvider = Callable[[str], str]

# 与最近消息相似度达到该阈值即视为重复
_REPEAT_THRESHOLD = 0.8
_REPEAT_WINDOW = 3


async def _safe_persist(persist: Persist) -> None:
    """状态保存失败只记日志，不影响发送流程与后续目标。"""
    try:
        await persist()
    except Exception:
        logger.exception("陪伴插件状态保存失败")


async def consolidate_targets(
    engine: CompanionEngine,
    llm: Any,
    targets: list[str],
    *,
    persist: Persist,
) -> int:
    """整理达到阈值的用户记忆，返回成功整理的数量。"""
    if llm is None:
        return 0
    consolidated = 0
    for target_id in targets:
        if not engine.should_consolidate(target_id):
            continue
        try:
            if await engine.consolidate(target_id, llm):
                consolidated += 1
        except Exception:
            logger.exception("角色记忆整理失败: target=%s", target_id)
    if consolidated:
        await _safe_persist(persist)
    return consolidated


def _is_repetitive(reply: str, profile: UserState) -> bool:
    """与最近几条角色消息高度相似时视为重复。"""
    tokens = tokenize(reply)
    if not tokens:
        return True
    checked = 0
    for turn in reversed(profile.turns):
        if turn.role != "assistant":
            continue
        if overlap(tokens, tokenize(turn.text)) >= _REPEAT_THRESHOLD:
            return True
        checked += 1
        if checked >= _REPEAT_WINDOW:
            break
    return False


async def _generate(
    llm: Any,
    target_id: str,
    system_prompt: str,
    user_prompt: str,
    *,
    hint: str = "",
) -> str:
    prompt = user_prompt if not hint else f"{user_prompt}\n（{hint}）"
    reply = await llm.chat(
        message=prompt,
        message_type="private",
        group_id=0,
        user_id=target_id,
        user_name="",
        system_prompt=system_prompt,
    )
    return str(reply or "").strip()


async def send_due_messages(
    *,
    engine: CompanionEngine,
    llm: Any,
    connection: Any,
    config: CompanionConfig,
    targets: list[str],
    started_at: float,
    persist: Persist,
    diary_provider: DiaryProvider | None = None,
) -> int:
    """向满足条件的用户发送主动消息，返回发送成功数量。"""
    now_ts = time.time()
    today = datetime.now().astimezone().date().isoformat()
    sent = 0
    for target_id in targets:
        # 只读取状态，不刷新 last_seen：否则空闲判断永远不成立
        profile = engine.store.get(target_id, now_ts)
        activity_ts = max(profile.last_seen, started_at)
        if not engine.can_send(target_id, now_ts, today, activity_ts):
            continue

        system_prompt, user_prompt = engine.proactive_prompt(
            target_id,
            now_ts,
            diary=diary_provider(target_id) if diary_provider else "",
        )
        try:
            reply = await _generate(llm, target_id, system_prompt, user_prompt)
            if not reply:
                continue
            if _is_repetitive(reply, profile):
                reply = await _generate(
                    llm,
                    target_id,
                    system_prompt,
                    user_prompt,
                    hint="换一种说法或换个话题，不要重复最近说过的话",
                )
                if not reply or _is_repetitive(reply, profile):
                    logger.info("主动消息与最近内容重复，跳过: target=%s", target_id)
                    continue
            if len(reply) > 180:
                reply = reply[:179].rstrip() + "…"

            result = await connection.send_private_msg(
                user_id=target_id,
                message=reply,
            )
            if isinstance(result, dict) and (
                result.get("status") == "failed"
                or result.get("retcode", 0) not in (0, "0", None)
            ):
                raise RuntimeError(f"消息发送失败: {result}")
        except Exception as error:
            engine.mark_send_failure(
                target_id, now_ts, f"{type(error).__name__}: {error}"
            )
            await _safe_persist(persist)
            logger.exception("主动私聊消息发送失败: target=%s", target_id)
            continue

        # 已成功送达：后续状态写入失败不应被记为发送失败
        engine.mark_sent(target_id, now_ts, today)
        engine.mark_send_success(target_id, now_ts)
        engine.observe_bot_message(target_id, reply, now_ts)
        await _safe_persist(persist)
        sent += 1
        logger.info("主动私聊消息已发送: target=%s", target_id)
    return sent
