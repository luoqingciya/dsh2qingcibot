# DeepSeek 鲸鱼娘主动陪伴插件

面向 Qingci-Bot 的独立 SDK 插件：仅在私聊中提供 `/dsh` 对话与主动问候，并具备分层记忆、上下文感知和情绪调节能力。

## 功能

- **私聊对话**：`/dsh <内容>` 调用 Qingci-Bot 的 LLM，使用鲸鱼娘人设回复。
- **分层记忆**：短期保留最近对话原文；长期从对话中提取稳定事实（称呼、偏好、近况、计划），按相关度召回；每累计若干轮由模型整理一次概述。
- **上下文感知**：提示词包含当前时间与时段、认识天数、互动次数、关系阶段，以及最近话题。
- **情绪模型**：鲸鱼娘自身情绪随互动变化并随时间回归基线；用户情绪低落、疲惫或出现高风险表达时，切换为支持性回应。
- **主动陪伴**：按用户白名单、安静时段、空闲时长、最小间隔与每日上限发送简短问候，话题优先承接最近对话；发送失败按 5/15/60 分钟退避，连续失败进入冷却。
- **隐私控制**：`/dsh 记忆` 查看、`/dsh 记住` 显式写入、`/dsh 忘记` 删除、`/dsh 导出` 带走数据；暂停主动消息后不再发送。
- **私人日记**：每天对话结束后，鲸鱼娘会依据当天真实对话、记忆与心情写一篇第一人称日记；`/dsh 日记` 查看、`/dsh 日记 写` 立即写、`/dsh 日记 清空` 删除。
- **管理与观测**：插件管理页展示用户数、记忆数、今日发送、暂停与失败情况（`/api/plugin-web/whale_companion/stats`）。

## 人设

鲸鱼娘是住在深蓝海沟与聊天窗口之间的虚拟鲸鱼：好奇人类生活，有稳定偏好（下雨、鲸歌、深夜还亮着的便利店），但不让偏好压过对方的话题。说话像熟人聊天——短句、口语、默认不超过三句；先听懂再回应，一次最多问一个问题；关系分寸随活跃天数与认识天数分层，初识不使用昵称，熟悉后才更随意。

边界明确：不声称自己是真人、不编造共同经历、不制造焦虑或诱导依赖、不索取敏感信息；医疗、法律、财务或心理危机场景只给审慎的一般信息，并建议寻求专业帮助。

## 安装

将仓库根目录（包含 `__init__.py` 和 `plugin.json`）放入 Qingci-Bot 的外部插件目录，例如：

```text
Qingci-Bot-CE/instances/default/plugins/whale_companion/
```

插件要求 Qingci-Bot 提供 `qingci-plugin-sdk>=1.13.9`，不引入第三方运行时依赖。

## DeepSeek 设置

插件通过 Qingci-Bot 注入的 `self.llm` 调用大模型，不单独保存 API Key，也不修改 Bot 的模型配置。要使用 DeepSeek，请先在 Qingci-Bot 的「LLM 配置」中配置 DeepSeek 服务商、模型和 API Key。此设置是 Bot 级别的，可能同时影响其他使用同一 LLM 服务的功能；插件加载时若检测到当前模型未标识为 DeepSeek，会写入提醒日志，但仍允许使用当前模型。

鲸鱼娘人设、记忆与情绪约束通过本插件每次请求的 system prompt 注入，仅对该插件发起的对话生效。

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
    memory_turns: 12
    memory_facts: 24
    memory_summary_every: 4
    emotion_decay_hours: 12
    memory_retention_days: 90
    diary_enabled: true
    diary_hour: 23
    diary_max_entries: 30
```

| 字段 | 默认值 | 说明 |
| --- | ---: | --- |
| `enabled` | `true` | 是否允许定时主动陪伴 |
| `private_targets` | `[]` | 允许主动私聊的用户 ID，使用字符串 |
| `interval_hours` | `8` | 同一用户两次主动消息的最短间隔，1–168 小时 |
| `idle_minutes` | `60` | 用户无新私聊消息达到此时长后才可主动联系，5–1440 分钟 |
| `daily_limit` | `2` | 每个用户每天最多主动消息数，1–5 |
| `active_start_hour` | `9` | 本地时间允许开始的小时（含） |
| `active_end_hour` | `22` | 本地时间允许结束的小时（不含） |
| `memory_turns` | `12` | 每位用户保留的最近对话轮数，2–40 |
| `memory_facts` | `24` | 长期记忆条目上限，0–100；`0` 关闭长期记忆 |
| `memory_summary_every` | `4` | 每累计多少轮用户消息整理一次记忆，0–20；`0` 关闭 |
| `emotion_decay_hours` | `12` | 情绪回归基线的半衰期，1–168 小时 |
| `memory_retention_days` | `90` | 记忆与对话保留天数，0–3650；`0` 表示不自动清理 |
| `diary_enabled` | `true` | 是否让鲸鱼娘按日写私人日记 |
| `diary_hour` | `23` | 当天几点之后可以写日记（本地时间） |
| `diary_max_entries` | `30` | 每位用户保留的日记篇数上限，5–100 |

`active_start_hour` 与 `active_end_hour` 相同代表不发送主动消息。调度器每 5 分钟检查一次目标；重启后会先等待完整的 `idle_minutes`，避免启动即推送。发送时间、每日计数、记忆与情绪状态保存在插件的 `data_dir/state.json`。

## 记忆与隐私

记忆分为三层：

1. **短期记忆**：最近 `memory_turns` 轮对话原文，直接进入提示词；
2. **长期记忆**：本地规则抽取的稳定事实，用 BM25 按相关度召回，并叠加新近程度与命中次数；
3. **记忆概述**：每累计 `memory_summary_every` 轮用户消息，调用一次模型把近期对话压缩为不超过 120 字的概述。

称呼、居住地、生日、职业等单值信息使用槽位覆盖：新说法会替换旧说法，不会出现「住在杭州」与「搬到上海」同时被召回。超过 `memory_retention_days` 的事实与对话会在定时任务中清理，相关概述同时失效并重新生成。

记忆整理直接使用宿主 LLM 适配器，不会把内部整理提示词写入用户会话历史或数据库。整理失败时保留本地规则抽取的结果，不影响正常对话。状态文件采用原子写入，损坏时自动备份为 `state.corrupt.json` 并重新开始；活跃消息触发的写入会合并为 3 秒一次，显式命令与主动发送立即落盘。

请只把明确同意接收主动消息的用户加入 `private_targets`。用户可随时用 `/dsh 忘记` 清除记忆、`/dsh 导出` 获取记忆快照、`/dsh 暂停` 停止主动消息。

## 日记

每天达到 `diary_hour` 后，调度任务会为当天有过对话、且尚未写日记的用户生成一篇 120–200 字的第一人称日记。日记只依据当天对话、长期记忆与情绪，不编造未发生的事；通过 LLM 适配器生成，不写入聊天历史，也不会主动发给用户。

日记独立保存在 `data_dir/diary.json`，每位用户最多 `diary_max_entries` 篇，并随 `memory_retention_days` 一起过期。最近一篇会以「【最近的日记】」注入对话上下文，让她的回应保持连续性。

- `/dsh 日记`：查看最近三篇
- `/dsh 日记 写`：立即写一篇
- `/dsh 日记 清空`：删除全部日记
- `/dsh 忘记`：清空记忆时一并清空日记

## 使用

- `/dsh`：开始聊天
- `/dsh 今天有点累`：带入文字进行对话
- `/dsh 记忆`：查看已记住的内容
- `/dsh 记住 我下周三考试`：显式写入一条长期记忆
- `/dsh 忘记 [关键词]`：删除相关记忆；不带关键词时清空全部
- `/dsh 导出`：导出当前记忆快照（JSON）
- `/dsh 日记`：查看鲸鱼娘最近的日记
- `/dsh 状态`：查看主动陪伴状态、当前心情与互动次数
- `/dsh 暂停` / `/dsh 继续`：管理当前会话的主动消息

对话由 Qingci-Bot 的 LLM 管理器完成；主动陪伴通过注入的 OneBot 连接发送。未启用调度器时，主动消息不可用，但对话命令仍然可用。插件命令只在私聊中响应。

## 管理与观测

插件加载时会按 SDK 约定注册管理页（`register_page`）与统计接口（`register_api`）：

- 页面：插件管理页的「鲸鱼娘」入口，静态文件位于 `web/index.html`；
- 接口：`GET /api/plugin-web/whale_companion/stats`，返回用户数、记忆条数、今日主动消息、暂停数、失败数，以及最多 100 条脱敏用户行。

统计只读，不提供远程修改配置或代发消息的接口。启用 API 鉴权时，宿主会以 `token` 参数把密钥传给页面，页面再以 `X-API-Key` 调用接口。

## 模块结构

```text
whale_companion/
├── __init__.py     # 插件类：matcher 注册、命令路由、主动发送、管理页/API
├── config.py       # 配置字段、校验与配置表单 schema
├── state.py        # state.json 持久化、旧版迁移、用户状态模型
├── diary.py        # 私人日记：生成、独立存储、保留期
├── memory.py       # 短期对话、事实抽取、BM25 召回、记忆整理
├── emotion.py      # 情绪状态、衰减、否定语义、高风险识别
├── persona.py      # 人设、上下文、记忆与情绪的提示词组装
├── commands.py     # /dsh 参数解析与固定文案
├── companion.py    # 陪伴业务逻辑（不依赖 SDK，可独立测试）
├── proactive.py    # 定时任务侧：记忆整理、去重与主动发送
├── web/index.html  # 插件管理页
├── plugin.json
├── CHANGELOG.md
├── .github/workflows/ci.yml
└── tests/
```

## 开发检查

在已安装 `Plugins-SDK` 的 Python 环境中执行：

```bash
python -m pytest tests
ruff check . && ruff format --check .
mypy --ignore-missing-imports __init__.py config.py state.py diary.py \
  memory.py emotion.py persona.py commands.py companion.py proactive.py
```

CI 在 Python 3.10–3.13 上运行同样的三项检查，配置见 `.github/workflows/ci.yml`。
