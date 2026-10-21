"""命令解析与固定文案。

把 ``/dsh`` 的参数解析从插件类中独立出来，便于单测覆盖所有分支，
也避免入口文件堆积文案。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from .config import DEFAULT_PERSONA_NAME

CommandKind = Literal[
    "help",
    "status",
    "pause",
    "resume",
    "memory",
    "forget",
    "remember",
    "export",
    "diary",
    "diagnose",
    "chat",
]

# 导出文本上限，避免超过平台单条消息长度限制
_MAX_EXPORT_CHARS = 3500


def default_chat_message(name: str = DEFAULT_PERSONA_NAME) -> str:
    return f"{name.strip() or DEFAULT_PERSONA_NAME}，陪我聊聊天吧。"


def help_text(name: str = DEFAULT_PERSONA_NAME) -> str:
    who = name.strip() or DEFAULT_PERSONA_NAME
    return (
        f"{who}可以陪你聊天，也会记住一些关于你的事。\n"
        "· /dsh <内容>：开始对话\n"
        "· /dsh 记忆：查看我记得的内容\n"
        "· /dsh 记住 <内容>：让我记住一件事\n"
        "· /dsh 忘记 [关键词]：清除记忆\n"
        "· /dsh 导出：导出我的记忆\n"
        f"· /dsh 日记：看{who}最近的日记\n"
        "· /dsh 诊断：确认插件人设是否生效\n"
        "· /dsh 状态：查看主动陪伴与当前心情\n"
        "· /dsh 暂停 / /dsh 继续：管理主动消息"
    )


_EXACT_COMMANDS: dict[str, CommandKind] = {
    "帮助": "help",
    "help": "help",
    "?": "help",
    "状态": "status",
    "status": "status",
    "暂停": "pause",
    "停止": "pause",
    "别打扰": "pause",
    "关闭主动陪伴": "pause",
    "继续": "resume",
    "开启": "resume",
    "恢复": "resume",
    "记忆": "memory",
    "memory": "memory",
    "回忆": "memory",
    "导出": "export",
    "export": "export",
    "诊断": "diagnose",
    "diag": "diagnose",
    "diagnose": "diagnose",
}


@dataclass(frozen=True)
class Command:
    """解析后的命令；``argument`` 对 chat/forget/remember 有意义。"""

    kind: CommandKind
    argument: str = ""


def parse(args: str) -> Command:
    """把 ``/dsh`` 之后的文本解析为命令。

    未知文本一律按对话处理，保持「直接说一句话就能聊」的体验。
    """
    text = (args or "").strip()
    if not text:
        return Command("chat", "")
    exact = _EXACT_COMMANDS.get(text.casefold())
    if exact is not None:
        return Command(exact)
    lowered = text.casefold()
    if lowered == "diary" or text == "日记":
        return Command("diary")
    for prefix in ("日记 ", "diary "):
        if lowered.startswith(prefix):
            return Command("diary", text[len(prefix) :].strip())
    if text.startswith("忘记"):
        return Command("forget", text[2:].strip())
    if text.startswith("记住"):
        return Command("remember", text[2:].strip())
    return Command("chat", text)


def status_text(
    *,
    enabled: bool,
    opted_out: bool,
    emotion: str,
    memory_count: int,
    interactions: int,
) -> str:
    return (
        f"主动陪伴：{'已启用' if enabled else '已关闭'}；"
        f"本会话：{'已暂停' if opted_out else '未暂停'}。\n"
        f"此刻心情：{emotion}；已记住 {memory_count} 条；互动 {interactions} 次。\n"
        "主动消息仅发给管理员配置的用户，并遵守免打扰时段与活跃检测。"
    )


def forget_text(removed: int) -> str:
    if removed <= 0:
        return "我没有找到相关记忆，可能是还没有记住过这件事。"
    return f"已经忘记 {removed} 条记忆。剩下的内容可以用 /dsh 记忆 查看。"


def export_text(payload: dict[str, Any]) -> str:
    """把记忆快照导出为可复制的 JSON 文本。

    平台单条消息长度有限，超长时先省略对话记录、再压缩事实条数，
    保证返回内容仍是合法 JSON。
    """
    data = dict(payload)
    facts = data.get("facts")
    if isinstance(facts, list):
        data["facts"] = facts[:50]
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) > _MAX_EXPORT_CHARS:
        data["turns"] = []
        text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) > _MAX_EXPORT_CHARS and isinstance(data.get("facts"), list):
        data["facts"] = data["facts"][:10]
        text = json.dumps(data, ensure_ascii=False, indent=2)
    return "记忆导出（JSON）：\n" + text
