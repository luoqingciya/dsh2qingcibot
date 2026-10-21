from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest
from dsh2qingcibot import WhaleCompanionPlugin
from dsh2qingcibot.config import CompanionConfig
from dsh2qingcibot.state import Fact, StateStore, Turn
from qingci_plugin_sdk import MatcherContext, paths


class FakeLLM:
    def __init__(self, reply: str = "鲸鱼娘来和你打个招呼。") -> None:
        self.reply = reply
        self.calls: list[dict] = []
        self.adapter = None

    async def chat(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return self.reply


class FakeConnection:
    def __init__(self) -> None:
        self.private_messages: list[dict] = []

    async def send_private_msg(self, **kwargs) -> dict:
        self.private_messages.append(kwargs)
        return {"status": "ok", "retcode": 0}


class FailingConnection(FakeConnection):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def send_private_msg(self, **kwargs) -> dict:
        self.attempts += 1
        raise RuntimeError("network down")


class SequenceLLM(FakeLLM):
    def __init__(self, replies: list[str]) -> None:
        super().__init__()
        self.replies = list(replies)

    async def chat(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return self.replies.pop(0) if self.replies else ""


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, func, **kwargs) -> None:
        self.jobs.append({"func": func, **kwargs})

    def remove_jobs_by_owner(self, owner: str) -> None:
        self.jobs = [job for job in self.jobs if job.get("owner") != owner]


class FakeAdapter:
    async def chat_detail(self, **kwargs):
        return type(
            "Result",
            (),
            {"content": '{"summary": "用户在准备考试", "facts": ["计划 下周三考试"]}'},
        )()


class DiaryAdapter:
    async def chat_detail(self, **kwargs):
        return type(
            "Result",
            (),
            {"content": "今天和对方聊了考试，心里有点紧，也替他期待明天。"},
        )()


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_data_root", tmp_path, raising=False)
    instance = WhaleCompanionPlugin()
    instance.matchers = []
    instance._started_at = 1.0
    instance.llm = FakeLLM()
    instance.connection = FakeConnection()
    monkeypatch.setattr(instance, "_within_active_hours", lambda now, start, end: True)
    instance._ensure_runtime()
    yield instance
    instance._cancel_pending_persist()


def private_ctx(
    args: str = "",
    *,
    user_id: str = "123456",
    message_id: str = "",
    plain_text: str | None = None,
) -> MatcherContext:
    text = plain_text if plain_text is not None else (f"/dsh {args}".strip())
    return MatcherContext(
        user_id=user_id,
        message_type="private",
        args=args,
        message_id=message_id,
        plain_text=text,
    )


@pytest.mark.asyncio
async def test_on_load_registers_private_matchers_and_schedule(plugin) -> None:
    scheduler = FakeScheduler()
    plugin.scheduler = scheduler

    await plugin.on_load()

    assert len(plugin.matchers) == 3
    assert plugin.matchers[0].priority == 0
    assert plugin.matchers[0].block is False
    assert plugin.matchers[2].priority == 10
    assert plugin.matchers[2].block is True
    assert scheduler.jobs[0]["job_id"] == "proactive_companionship"
    assert scheduler.jobs[0]["minutes"] == 5

    group_ctx = MatcherContext(message_type="group", plain_text="/dsh 你好")
    private_ctx_obj = MatcherContext(message_type="private", plain_text="/dsh 你好")
    legacy_ctx = MatcherContext(message_type="private", plain_text="/鲸鱼娘 你好")
    assert not await plugin.matchers[1].rule.check(None, {}, group_ctx)
    assert await plugin.matchers[1].rule.check(None, {}, private_ctx_obj)
    assert not await plugin.matchers[1].rule.check(None, {}, legacy_ctx)
    assert plugin.matchers[1].meta["command"] == "dsh"


@pytest.mark.asyncio
async def test_group_command_is_rejected_without_calling_llm(plugin) -> None:
    ctx = MatcherContext(
        user_id="123456", group_id="789", message_type="group", args="今天有点累"
    )

    reply = await plugin._handle_command(ctx)

    assert "只支持私聊" in reply
    assert plugin.llm.calls == []


@pytest.mark.asyncio
async def test_help_and_status_commands(plugin) -> None:
    help_reply = await plugin._handle_command(private_ctx("帮助"))
    status_reply = await plugin._handle_command(private_ctx("状态"))

    assert "/dsh 记忆" in help_reply
    assert "此刻心情" in status_reply
    assert plugin.llm.calls == []


@pytest.mark.asyncio
async def test_remember_and_memory_commands(plugin) -> None:
    await plugin._handle_command(private_ctx("记住 我下周三考试"))

    digest = await plugin._handle_command(private_ctx("记忆"))

    assert "考试" in digest
    assert "考试" in (plugin.engine.memory_digest("123456"))


@pytest.mark.asyncio
async def test_forget_command_removes_memory(plugin) -> None:
    await plugin._handle_command(private_ctx("记住 我下周三考试"))

    reply = await plugin._handle_command(private_ctx("忘记 考试"))

    assert "忘记" in reply
    assert plugin.engine.store.peek("123456").facts == []


@pytest.mark.asyncio
async def test_pause_and_resume_persist_opt_out(plugin, tmp_path) -> None:
    await plugin._handle_command(private_ctx("暂停"))
    assert plugin.engine.store.peek("123456").opt_out is True
    assert (tmp_path / "plugins" / "whale_companion" / "state.json").is_file()

    await plugin._handle_command(private_ctx("继续"))
    assert plugin.engine.store.peek("123456").opt_out is False


@pytest.mark.asyncio
async def test_chat_records_memory_and_uses_emotion_prompt(plugin) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])
    await plugin._handle_command(private_ctx("记住 我下周三考试"))

    reply = await plugin._handle_command(private_ctx("今天有点累", message_id="m2"))

    assert reply == "鲸鱼娘来和你打个招呼。"
    call = plugin.llm.calls[0]
    assert call["message"] == "今天有点累"
    assert call["message_type"] == "private"
    assert "【你此刻的心情】" in call["system_prompt"]
    assert "考试" in call["system_prompt"]
    profile = plugin.engine.store.peek("123456")
    assert profile is not None
    assert profile.interactions == 1
    assert [turn.role for turn in profile.turns] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_activity_listener_deduplicates_command_message(plugin) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])
    ctx = private_ctx("今天有点累", message_id="m1")

    await plugin._track_activity(ctx)
    await plugin._handle_command(ctx)

    profile = plugin.engine.store.peek("123456")
    assert profile is not None
    assert profile.interactions == 1
    assert [turn.role for turn in profile.turns].count("user") == 1


@pytest.mark.asyncio
async def test_activity_listener_ignores_command_text(plugin) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])

    await plugin._track_activity(private_ctx("状态", message_id="m1"))

    assert plugin.engine.store.peek("123456") is None


@pytest.mark.asyncio
async def test_activity_listener_ignores_non_targets_and_groups(plugin) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])

    await plugin._track_activity(
        private_ctx("hi", user_id="999", plain_text="hi", message_id="m1")
    )
    await plugin._track_activity(
        MatcherContext(
            user_id="222",
            group_id="789",
            message_type="group",
            plain_text="hello",
        )
    )

    assert plugin.engine.store.peek("999") is None


@pytest.mark.asyncio
async def test_proactive_message_sends_once_per_interval(plugin, tmp_path) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])
    profile = plugin.engine.store.get("123456", 1.0)
    profile.last_seen = 1.0

    await plugin._proactive_tick()
    await plugin._proactive_tick()

    assert len(plugin.connection.private_messages) == 1
    assert plugin.connection.private_messages[0]["user_id"] == "123456"
    assert plugin.connection.private_messages[0]["message"] == "鲸鱼娘来和你打个招呼。"
    assert "【此刻】" in plugin.llm.calls[0]["system_prompt"]
    assert profile.last_sent > 0
    assert profile.sent_count == 1
    state_file = tmp_path / "plugins" / "whale_companion" / "state.json"
    assert json.loads(state_file.read_text(encoding="utf-8"))["version"] == 2


@pytest.mark.asyncio
async def test_proactive_respects_opt_out_and_disabled(plugin) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])
    profile = plugin.engine.store.get("123456", 1.0)
    profile.last_seen = 1.0
    profile.opt_out = True

    await plugin._proactive_tick()
    assert plugin.connection.private_messages == []

    profile.opt_out = False
    plugin.plugin_config = CompanionConfig(enabled=False, private_targets=["123456"])
    await plugin._proactive_tick()
    assert plugin.connection.private_messages == []


@pytest.mark.asyncio
async def test_proactive_respects_idle_window(plugin) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])
    profile = plugin.engine.store.get("123456", time.time())
    profile.last_seen = time.time()

    await plugin._proactive_tick()

    assert plugin.connection.private_messages == []


@pytest.mark.asyncio
async def test_consolidation_runs_after_configured_turns(plugin) -> None:
    plugin.plugin_config = CompanionConfig(memory_summary_every=1)
    plugin.llm.adapter = FakeAdapter()

    await plugin._handle_command(private_ctx("我下周三要考试", message_id="m1"))

    profile = plugin.engine.store.peek("123456")
    assert profile is not None
    assert profile.summary == "用户在准备考试"
    assert profile.pending_turns == 0


@pytest.mark.asyncio
async def test_tick_consolidates_memory_even_when_proactive_disabled(plugin) -> None:
    plugin.plugin_config = CompanionConfig(
        enabled=False, private_targets=["123456"], memory_summary_every=1
    )
    plugin.llm.adapter = FakeAdapter()
    now = time.time()
    profile = plugin.engine.store.get("123456", now)
    plugin.engine.observe_user_message("123456", "我下周三要考试", now)
    plugin.engine.observe_bot_message("123456", "那我陪你复习", now)
    assert profile.pending_turns == 1

    await plugin._proactive_tick()

    assert profile.summary == "用户在准备考试"
    assert profile.pending_turns == 0
    assert plugin.connection.private_messages == []


@pytest.mark.asyncio
async def test_export_command_returns_json_memory(plugin) -> None:
    await plugin._handle_command(private_ctx("记住 我下周三考试"))

    reply = await plugin._handle_command(private_ctx("导出"))

    assert "记忆导出" in reply
    assert "考试" in reply


@pytest.mark.asyncio
async def test_stats_api_aggregates_and_masks_user_ids(plugin) -> None:
    await plugin._handle_command(private_ctx("记住 我喜欢下雨天"))

    data = await plugin._api_stats(None)

    assert data["users"] == 1
    assert data["facts"] >= 1
    assert data["rows"][0]["user_id"] == "12***56"


@pytest.mark.asyncio
async def test_on_load_registers_management_page_and_stats_api(plugin) -> None:
    await plugin.on_load()

    assert plugin._pages
    assert plugin._pages[0]["title"] == "鲸鱼娘"
    assert any(api["path"] == "stats" for api in plugin._apis)


@pytest.mark.asyncio
async def test_debounced_persist_merges_and_force_flushes(plugin, tmp_path) -> None:
    await plugin._persist(force=False)
    assert plugin._persist_task is not None

    await plugin._persist()

    assert plugin._persist_task is None
    assert (tmp_path / "plugins" / "whale_companion" / "state.json").is_file()


@pytest.mark.asyncio
async def test_proactive_failure_backoff_skips_next_attempt(plugin) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])
    profile = plugin.engine.store.get("123456", 1.0)
    profile.last_seen = 1.0
    connection = FailingConnection()
    plugin.connection = connection

    await plugin._proactive_tick()
    assert profile.failure_count == 1
    assert profile.retry_after > time.time()

    await plugin._proactive_tick()
    assert connection.attempts == 1


@pytest.mark.asyncio
async def test_proactive_skips_repetitive_message(plugin) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])
    profile = plugin.engine.store.get("123456", 1.0)
    profile.last_seen = 1.0
    now = time.time()
    plugin.engine.observe_bot_message("123456", "今天下雨了，记得带伞", now)
    plugin.llm.reply = "今天下雨了，记得带伞"

    await plugin._proactive_tick()

    assert plugin.connection.private_messages == []


@pytest.mark.asyncio
async def test_proactive_rewrites_repetitive_message_once(plugin) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])
    profile = plugin.engine.store.get("123456", 1.0)
    profile.last_seen = 1.0
    now = time.time()
    plugin.engine.observe_bot_message("123456", "今天下雨了，记得带伞", now)
    plugin.llm = SequenceLLM(["今天下雨了，记得带伞", "考试准备得怎么样了？"])

    await plugin._proactive_tick()

    assert plugin.connection.private_messages[0]["message"] == "考试准备得怎么样了？"
    assert len(plugin.llm.calls) == 2


@pytest.mark.asyncio
async def test_tick_prunes_expired_memory_without_targets(plugin) -> None:
    now = time.time()
    profile = plugin.engine.store.get("123456", now)
    profile.facts = [Fact("旧记忆", now - 200 * 86400)]
    profile.turns = [Turn("user", "旧对话", now - 200 * 86400)]
    plugin.plugin_config = CompanionConfig(private_targets=[])

    await plugin._proactive_tick()

    assert profile.facts == []
    assert profile.turns == []


@pytest.mark.asyncio
async def test_persist_failure_does_not_mark_send_failure(plugin, monkeypatch) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])
    profile = plugin.engine.store.get("123456", 1.0)
    profile.last_seen = 1.0

    async def boom() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(plugin, "_persist", boom)

    await plugin._proactive_tick()

    assert len(plugin.connection.private_messages) == 1
    assert profile.failure_count == 0
    assert profile.retry_after == 0.0


@pytest.mark.asyncio
async def test_diary_empty_and_write_and_clear(plugin) -> None:
    empty = await plugin._handle_command(private_ctx("日记"))
    assert "还没有日记" in empty

    plugin.llm.adapter = DiaryAdapter()
    written = await plugin._handle_command(private_ctx("日记 写"))
    assert "写好了" in written
    assert len(plugin._diary.entries("123456")) == 1

    listed = await plugin._handle_command(private_ctx("日记"))
    assert "最近的日记" in listed
    assert "考试" in listed

    cleared = await plugin._handle_command(private_ctx("日记 清空"))
    assert "清空" in cleared
    assert plugin._diary.entries("123456") == []


@pytest.mark.asyncio
async def test_diary_is_written_once_per_day_by_tick(plugin) -> None:
    plugin.plugin_config = CompanionConfig(diary_hour=0)
    plugin.llm.adapter = DiaryAdapter()
    now = time.time()
    plugin.engine.observe_user_message("123456", "今天有点累", now)

    await plugin._proactive_tick()
    await plugin._proactive_tick()

    assert len(plugin._diary.entries("123456")) == 1


@pytest.mark.asyncio
async def test_forget_all_clears_diary(plugin) -> None:
    plugin.llm.adapter = DiaryAdapter()
    await plugin._handle_command(private_ctx("日记 写"))

    await plugin._handle_command(private_ctx("忘记"))

    assert plugin._diary.entries("123456") == []


@pytest.mark.asyncio
async def test_chat_prompt_includes_latest_diary(plugin) -> None:
    plugin.llm.adapter = DiaryAdapter()
    await plugin._handle_command(private_ctx("日记 写"))
    plugin.llm = FakeLLM()

    await plugin._handle_command(private_ctx("今天有点累", message_id="m2"))

    assert "【最近的日记】" in plugin.llm.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_auto_reply_rule_only_matches_target_private_text(plugin) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])
    rule = plugin._auto_reply_rule()

    target = MatcherContext(
        user_id="123456", message_type="private", plain_text="今天好累"
    )
    other = MatcherContext(user_id="999", message_type="private", plain_text="今天好累")
    command = MatcherContext(
        user_id="123456", message_type="private", plain_text="/dsh 你好"
    )

    assert await rule.check(None, {}, target) is True
    assert await rule.check(None, {}, other) is False
    assert await rule.check(None, {}, command) is False


@pytest.mark.asyncio
async def test_auto_reply_rule_respects_switch(plugin) -> None:
    plugin.plugin_config = CompanionConfig(
        private_targets=["123456"], private_auto_reply=False
    )
    rule = plugin._auto_reply_rule()
    ctx = MatcherContext(user_id="123456", message_type="private", plain_text="在吗")

    assert await rule.check(None, {}, ctx) is False


@pytest.mark.asyncio
async def test_auto_reply_uses_plugin_persona(plugin) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])
    ctx = MatcherContext(
        user_id="123456",
        message_type="private",
        plain_text="今天好累",
        message_id="m1",
    )

    reply = await plugin._handle_private_chat(ctx)

    assert reply == "鲸鱼娘来和你打个招呼。"
    assert "你是鲸鱼娘" in plugin.llm.calls[0]["system_prompt"]
    assert plugin.engine.store.peek("123456").interactions == 1


@pytest.mark.asyncio
async def test_diagnose_command_reports_persona_source(plugin) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])

    text = await plugin._handle_command(private_ctx("诊断"))

    assert plugin.version in text
    assert "插件注入" in text
    assert "接管白名单私聊：开" in text


@pytest.mark.asyncio
async def test_command_exception_returns_fallback(plugin, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin.engine, "system_prompt", boom)

    reply = await plugin._handle_command(private_ctx("你好"))

    assert "走神" in reply


@pytest.mark.asyncio
async def test_custom_persona_name_applies_to_help_prompt_and_diagnose(plugin) -> None:
    plugin.plugin_config = CompanionConfig(
        private_targets=["123456"], persona_name="小鲸"
    )

    help_reply = await plugin._handle_command(private_ctx("帮助"))
    await plugin._handle_command(private_ctx("你好", message_id="m1"))
    diagnose = await plugin._handle_command(private_ctx("诊断"))

    assert "小鲸" in help_reply
    assert "你是小鲸" in plugin.llm.calls[0]["system_prompt"]
    assert "角色名：小鲸" in diagnose


@pytest.mark.asyncio
async def test_state_survives_reload(plugin, tmp_path) -> None:
    plugin.plugin_config = CompanionConfig(private_targets=["123456"])
    await plugin._handle_command(private_ctx("记住 我喜欢下雨天"))

    store = StateStore(tmp_path / "plugins" / "whale_companion" / "state.json")
    store.load()

    restored = store.peek("123456")
    assert restored is not None
    assert any("下雨天" in fact.text for fact in restored.facts)


@pytest.mark.parametrize(
    ("hour", "start", "end", "expected"),
    [(9, 9, 22, True), (21, 9, 22, True), (22, 9, 22, False), (2, 22, 7, True)],
)
def test_active_hour_window(hour: int, start: int, end: int, expected: bool) -> None:
    now = datetime(2026, 10, 20, hour, 30, tzinfo=timezone.utc)

    assert WhaleCompanionPlugin._within_active_hours(now, start, end) is expected
