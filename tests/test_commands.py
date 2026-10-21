from __future__ import annotations

import json

import pytest
from dsh2qingcibot import commands


@pytest.mark.parametrize(
    ("args", "kind", "argument"),
    [
        ("", "chat", commands.DEFAULT_CHAT_MESSAGE),
        ("帮助", "help", ""),
        ("HELP", "help", ""),
        ("状态", "status", ""),
        ("暂停", "pause", ""),
        ("别打扰", "pause", ""),
        ("继续", "resume", ""),
        ("恢复", "resume", ""),
        ("记忆", "memory", ""),
        ("导出", "export", ""),
        ("export", "export", ""),
        ("日记", "diary", ""),
        ("日记 写", "diary", "写"),
        ("日记 清空", "diary", "清空"),
        ("diary write", "diary", "write"),
        ("忘记", "forget", ""),
        ("忘记 考试", "forget", "考试"),
        ("记住 我喜欢下雨天", "remember", "我喜欢下雨天"),
        ("今天有点累", "chat", "今天有点累"),
    ],
)
def test_parse_command(args: str, kind: str, argument: str) -> None:
    command = commands.parse(args)

    assert command.kind == kind
    assert command.argument == argument


def test_status_text_reports_state() -> None:
    text = commands.status_text(
        enabled=True,
        opted_out=False,
        emotion="愉快",
        memory_count=3,
        interactions=12,
    )

    assert "已启用" in text
    assert "未暂停" in text
    assert "愉快" in text
    assert "3 条" in text


def test_forget_text_handles_empty_result() -> None:
    assert "没有找到" in commands.forget_text(0)
    assert "2 条" in commands.forget_text(2)


def test_export_text_caps_facts() -> None:
    text = commands.export_text({"facts": [f"事实{index}" for index in range(60)]})

    assert "记忆导出" in text
    assert "事实49" in text
    assert "事实50" not in text


def test_export_text_truncates_long_payload_but_keeps_valid_json() -> None:
    payload = {
        "summary": "摘要" * 100,
        "facts": [f"事实{index}" + "内容" * 20 for index in range(50)],
        "turns": [{"role": "user", "text": "长" * 500} for _ in range(10)],
    }

    text = commands.export_text(payload)
    exported = json.loads(text.split("\n", 1)[1])

    assert len(text) <= 3600
    assert exported["turns"] == []
    assert len(exported["facts"]) <= 50
