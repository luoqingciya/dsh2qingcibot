# DeepSeek 鲸鱼娘主动陪伴插件

面向 Qingci-Bot 的独立 SDK 插件：仅支持私聊中的 `/鲸鱼娘 <内容>` 对话，以及按私聊用户、时段、空闲状态和频率限制的主动问候。

## 安装

将仓库根目录（包含 `__init__.py` 和 `plugin.json`）放入 Qingci-Bot 的外部插件目录，例如：

```text
Qingci-Bot-CE/instances/default/plugins/whale_companion/
```

插件要求 Qingci-Bot 提供 `qingci-plugin-sdk>=1.13.9`。本插件不引入第三方运行时依赖。

## DeepSeek 设置

插件通过 Qingci-Bot 注入的 `self.llm` 调用大模型，不单独保存 API Key，也不修改 Bot 的模型配置。要使用 DeepSeek，请先在 Qingci-Bot 的「LLM 配置」中配置 DeepSeek 服务商、模型和 API Key。此设置是 Bot 级别的，可能同时影响其他使用同一 LLM 服务的功能；插件加载时若检测到当前模型没有标识为 DeepSeek，会写入提醒日志，但仍允许对话使用当前模型。

鲸鱼娘人设通过本插件的 system prompt 覆盖，仅对该插件发起的对话生效。

## 插件配置

在 `config.yaml` 中配置 `plugins.whale_companion`。默认没有主动消息目标，因此不会主动联系任何人：

```yaml
plugins:
  whale_companion:
    enabled: true
    private_targets: ["123456789"]
    interval_hours: 8
    idle_minutes: 60
    daily_limit: 2
    active_start_hour: 9
    active_end_hour: 22
```

| 字段 | 默认值 | 说明 |
| --- | ---: | --- |
| `enabled` | `true` | 是否允许定时主动陪伴 |
| `private_targets` | `[]` | 允许主动私聊的用户 ID，使用字符串 |
| `interval_hours` | `8` | 同一用户两次主动消息的最短间隔，范围 1–168 小时 |
| `idle_minutes` | `60` | 用户无新私聊消息达到此时长后，才可主动联系，范围 5–1440 分钟 |
| `daily_limit` | `2` | 每个用户每天最多主动消息数，范围 1–5 |
| `active_start_hour` | `9` | 服务器本地时间允许开始的小时（含） |
| `active_end_hour` | `22` | 服务器本地时间允许结束的小时（不含） |

调度器每 5 分钟检查一次私聊用户。重启后会先等待完整的 `idle_minutes`，避免启动即推送。主动发送时间、每日计数和暂停名单保存在插件的 `data_dir/state.json` 中。`active_start_hour` 与 `active_end_hour` 相同代表不发送主动消息。

私聊目标必须已与机器人建立可发送私信的关系。请只填写明确同意接收主动消息的用户。用户可发送 `/鲸鱼娘 暂停` 停止私聊主动消息，发送 `/鲸鱼娘 继续` 恢复；配置中的 `enabled: false` 可全局关闭定时陪伴。插件命令也只在私聊中响应。

## 使用

- `/鲸鱼娘`：开始聊天
- `/鲸鱼娘 今天有点累`：带入文字进行对话
- `/whale help`：显示帮助
- `/鲸鱼娘 状态`：查看插件开关和当前会话的暂停状态
- `/鲸鱼娘 暂停` 或 `/鲸鱼娘 继续`：管理当前会话主动陪伴

对话由 Qingci-Bot 的 LLM 管理器完成；需要主动陪伴时，插件才会通过注入的 OneBot 连接发送消息。未启用调度器时，主动消息不可用，但对话命令仍然可用。

## 开发检查

在 `Plugins-SDK` 已安装的 Python 环境中执行：

```bash
python -m pytest tests
```
