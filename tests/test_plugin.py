from __future__ import annotations

from datetime import datetime, timezone

import pytest
from qingci_plugin_sdk import MatcherContext

from __init__ import WhaleCompanionPlugin


class FakeLLM:
    def __init__(self, reply: str = "鲸鱼娘来和你打个招呼。") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def chat(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return self.reply


class FakeConnection:
    def __init__(self) -> None:
        self.private_messages: list[dict] = []

    async def send_private_msg(self, **kwargs) -> dict:
        self.private_messages.append(kwargs)
        return {"status": "ok", "retcode": 0}


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, func, **kwargs) -> None:
        self.jobs.append({"func": func, **kwargs})

    def remove_jobs_by_owner(self, owner: str) -> None:
        self.jobs = [job for job in self.jobs if job.get("owner") != owner]


def make_plugin(tmp_path, monkeypatch) -> WhaleCompanionPlugin:
    plugin = WhaleCompanionPlugin()
    plugin.matchers = []
    plugin._started_at = 0
    monkeypatch.setattr(plugin, "_state_path", lambda: tmp_path / "state.json")
    plugin.llm = FakeLLM()
    plugin.connection = FakeConnection()
    monkeypatch.setattr(plugin, "_within_active_hours", lambda now, start, end: True)
    return plugin


def test_config_defaults_have_no_unsolicited_targets() -> None:
    config = WhaleCompanionPlugin.Config()

    assert config.enabled is True
    assert config.private_targets == []
    assert config.interval_hours == 8
    assert "group_targets" not in config.model_dump()
    assert "group_targets" not in config.model_json_schema()["properties"]
    assert config.idle_minutes == 60


def test_config_rejects_unsafe_intervals() -> None:
    with pytest.raises(ValueError):
        WhaleCompanionPlugin.Config(interval_hours=0)
    with pytest.raises(ValueError):
        WhaleCompanionPlugin.Config(daily_limit=6)


def test_load_state_discards_legacy_group_targets(tmp_path, monkeypatch) -> None:
    plugin = WhaleCompanionPlugin()
    path = tmp_path / "state.json"
    path.write_text(
        '{"last_sent":{"private:123":1,"group:456":2},'
        '"sent_today":{"private:123":{"date":"2026-10-20","count":1},'
        '"group:456":{"date":"2026-10-20","count":2}},'
        '"opt_out":["private:123","group:456"]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(plugin, "_state_path", lambda: path)

    plugin._load_state()

    assert plugin._state["last_sent"] == {"private:123": 1.0}
    assert plugin._state["sent_today"] == {
        "private:123": {"date": "2026-10-20", "count": 1}
    }
    assert plugin._state["opt_out"] == ["private:123"]


def test_target_normalization_deduplicates_ids() -> None:
    config = WhaleCompanionPlugin.Config(private_targets=["123", "123", " 456 "])

    assert WhaleCompanionPlugin._targets(config) == ["123", "456"]


@pytest.mark.asyncio
async def test_proactive_message_sends_once_and_persists_state(
    tmp_path, monkeypatch
) -> None:
    plugin = make_plugin(tmp_path, monkeypatch)
    plugin.plugin_config = plugin.Config(private_targets=["123456"])

    await plugin._proactive_tick()
    await plugin._proactive_tick()

    assert len(plugin.connection.private_messages) == 1
    assert plugin.connection.private_messages[0]["user_id"] == "123456"
    assert plugin.connection.private_messages[0]["message"] == "鲸鱼娘来和你打个招呼。"
    assert plugin.llm.calls[0]["system_prompt"]
    assert plugin._state["last_sent"]["private:123456"] > 0
    assert (tmp_path / "state.json").is_file()


@pytest.mark.asyncio
async def test_proactive_tick_respects_opt_out_and_disabled_config(
    tmp_path, monkeypatch
) -> None:
    plugin = make_plugin(tmp_path, monkeypatch)
    plugin.plugin_config = plugin.Config(private_targets=["123456"])
    plugin._state["opt_out"] = ["private:123456"]

    await plugin._proactive_tick()
    assert plugin.connection.private_messages == []

    plugin._state["opt_out"] = []
    plugin.plugin_config = plugin.Config(enabled=False, private_targets=["123456"])
    await plugin._proactive_tick()
    assert plugin.connection.private_messages == []


@pytest.mark.asyncio
async def test_on_load_registers_nonblocking_activity_listener_and_schedule(
    tmp_path, monkeypatch
) -> None:
    plugin = make_plugin(tmp_path, monkeypatch)
    scheduler = FakeScheduler()
    plugin.scheduler = scheduler

    await plugin.on_load()

    assert len(plugin.matchers) == 2
    assert plugin.matchers[0].priority == 0
    assert plugin.matchers[0].block is False
    assert len(scheduler.jobs) == 1
    assert scheduler.jobs[0]["job_id"] == "proactive_companionship"
    assert scheduler.jobs[0]["minutes"] == 5

    group_ctx = MatcherContext(message_type="group", plain_text="/dsh 你好")
    private_ctx = MatcherContext(message_type="private", plain_text="/dsh 你好")
    legacy_ctx = MatcherContext(message_type="private", plain_text="/鲸鱼娘 你好")
    assert not await plugin.matchers[1].rule.check(None, {}, group_ctx)
    assert await plugin.matchers[1].rule.check(None, {}, private_ctx)
    assert not await plugin.matchers[1].rule.check(None, {}, legacy_ctx)
    assert plugin.matchers[1].meta["command"] == "dsh"


@pytest.mark.asyncio
async def test_group_command_is_rejected_without_calling_llm(
    tmp_path, monkeypatch
) -> None:
    plugin = make_plugin(tmp_path, monkeypatch)
    ctx = MatcherContext(
        user_id="123456",
        group_id="789",
        message_type="group",
        args="今天有点累",
    )

    reply = await plugin._handle_command(ctx)

    assert "只支持私聊" in reply
    assert plugin.llm.calls == []


@pytest.mark.asyncio
async def test_legacy_group_target_config_is_ignored() -> None:
    config = WhaleCompanionPlugin.Config(group_targets=["987654321"])

    assert config.private_targets == []
    assert WhaleCompanionPlugin._targets(config) == []


@pytest.mark.asyncio
async def test_pause_command_persists_opt_out(tmp_path, monkeypatch) -> None:
    plugin = make_plugin(tmp_path, monkeypatch)
    ctx = MatcherContext(user_id="123456", message_type="private", args="暂停")

    reply = await plugin._handle_command(ctx)

    assert "暂停" in reply
    assert plugin._state["opt_out"] == ["private:123456"]
    assert (tmp_path / "state.json").is_file()


@pytest.mark.asyncio
async def test_activity_tracking_only_tracks_configured_private_targets(
    tmp_path, monkeypatch
) -> None:
    plugin = make_plugin(tmp_path, monkeypatch)
    plugin.plugin_config = plugin.Config(private_targets=["123456"])

    await plugin._track_activity(
        MatcherContext(
            user_id="123456", self_id="bot", message_type="private", plain_text="hi"
        )
    )
    await plugin._track_activity(
        MatcherContext(
            user_id="999", self_id="bot", message_type="private", plain_text="hi"
        )
    )
    await plugin._track_activity(
        MatcherContext(
            user_id="222",
            self_id="bot",
            group_id="789",
            message_type="group",
            plain_text="hello",
        )
    )

    assert set(plugin._last_activity) == {"private:123456"}


@pytest.mark.asyncio
async def test_command_routes_private_chat_to_shared_llm(tmp_path, monkeypatch) -> None:
    plugin = make_plugin(tmp_path, monkeypatch)
    ctx = MatcherContext(
        user_id="123456",
        sender={"nickname": "小明"},
        message_type="private",
        args="今天有点累",
    )

    reply = await plugin._handle_command(ctx)

    assert reply == "鲸鱼娘来和你打个招呼。"
    assert plugin.llm.calls[0]["message"] == "今天有点累"
    assert plugin.llm.calls[0]["message_type"] == "private"
    assert plugin.llm.calls[0]["system_prompt"]


@pytest.mark.parametrize(
    ("hour", "start", "end", "expected"),
    [(9, 9, 22, True), (21, 9, 22, True), (22, 9, 22, False), (2, 22, 7, True)],
)
def test_active_hour_window(hour: int, start: int, end: int, expected: bool) -> None:
    now = datetime(2026, 10, 20, hour, 30, tzinfo=timezone.utc)

    assert WhaleCompanionPlugin._within_active_hours(now, start, end) is expected
