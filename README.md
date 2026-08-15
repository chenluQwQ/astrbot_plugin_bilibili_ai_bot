# astrbot_plugin_bilibili_ai_bot（Fork 版本）

> B 站 AI Bot 插件 for [AstrBot](https://github.com/AstrBotDevs/AstrBot)。
> 本仓库是基于原作者项目维护的社区 Fork 版本，当前 Fork 默认分支包含本仓库的架构重构与 WebUI 改进。

## 🙏 感谢原作者 · Fork 版本新增内容

首先感谢原作者 [chenluQwQ/astrbot_plugin_bilibili_ai_bot](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot) 开源并持续维护这个项目。原项目提供了 B 站评论互动、私信、直播弹幕、主动行为、记忆、人格和番剧等完整基础能力，是本 Fork 版本继续开发的基础。

本仓库由 `zzz27578` 维护，**不是上游官方仓库**。我们尽量保持原有配置和使用方式兼容，并在原项目基础上增加以下内容：

- **分层运行时接入**：新增 `core/layered_runtime.py`，把事件、身份、安全、存储、媒体、人格和工具治理连接到插件生命周期中，同时保留原有低延迟事件处理路径。
- **统一事件模型与持久化状态**：新增 `core/adapter/`，为评论、私信、弹幕等事件提供统一入站模型、幂等记录、领取和状态审计；旧的 `core.event_adapter` 导入仍保留兼容入口。
- **安全与权限治理**：新增 `core/security/`，提供身份 namespace、会话域、记忆域、敏感内容处理、工具分级、调用审计和一次性写操作确认。
- **SQLite 存储层**：新增 `core/storage/` 和统一 schema，保存事件、动作、画像、记忆、媒体摘要、审计记录和运行状态，重启后仍可恢复关键状态。
- **人格与周期行为基础设施**：新增 `core/persona/`、`core/governance/`、`core/schedule/`，为人格状态、行为决策、内容治理和主动计划提供独立模块。
- **内置 AstrBot Plugin Page**：新增 `pages/bilibot/` 和 `core/webui_bridge.py`，在 AstrBot 内提供状态、配置、账号、二维码登录、人格、主动计划、记忆、媒体和安全信息的管理页面。
- **B 站登录与管理能力补充**：WebUI 可读取配置 schema、保存配置、查看账号状态、生成二维码、轮询登录和退出登录；B 站扫码登录仍可通过 `/bili登录` 使用。
- **测试覆盖补充**：增加事件适配、分层运行时、安全层和可靠性测试，当前本地测试结果为 `72 passed`。

这些改动的目标是：在不改变原版主要使用方式的前提下，让项目更容易维护、更容易观察运行状态，并降低跨会话数据污染、重复处理和高风险工具误调用的风险。

> 如果你要使用原始版本、查看上游变更或向上游反馈原项目问题，请前往原作者仓库；如果问题只在本 Fork 版本出现，请在本 Fork 仓库提交 Issue。

## ✨ Fork 版本当前能力

### B 站互动

- B 站评论自动回复和评论区 `@` 回复
- 动态评论区回复
- 图片识别和视频上下文分析（依赖相应视觉模型）
- B 站私信监听与回复，可配置只回复主人、白名单或全部安全用户
- 不明外链、IP 链接和疑似引流内容的隔离处理
- 直播弹幕监听与回复
- B 站链接、BV 号和分享卡片解析
- 可选的联网查询、图片生成和视频分析

### 记忆、画像和人格

- 语义记忆检索（需要 Embedding 配置时效果最佳）
- 用户画像、好感度、黑名单和人格演化
- 视频、动态、番剧和直播互动记录
- QQ 与 B 站 UID 绑定后的记忆互通接口
- 统一的会话、身份和记忆隔离策略

### 主动行为与周期任务

- 主动看视频、评分、点赞、投币、收藏、关注和评论
- 关注更新、搜索和视频池等候选来源
- 自动发布动态，可选 AI 配图
- 关注用户动态巡检
- 番剧搜索、观看、记忆和追番
- 每日计划、周期任务和周总结图片卡片
- 主动行为支持每日上限、时间计划、最小间隔和活跃度控制

### LLM 工具

插件会向 AstrBot 注册 B 站相关 FunctionTool。具体工具名称以当前 AstrBot 与插件注册结果为准，工具会按用途分为：

- 公共只读查询：搜索、读取公开信息、查询状态
- 私域只读查询：读取当前用户或会话相关记忆
- 写操作：评论、动态、私信分享等需要更高权限的动作

分层运行时会对工具进行身份、会话、scope、能力票据和审计检查。聊天模型不支持 tool calling 时，命令功能仍可单独使用。

## 🖥️ 内置 WebUI

Fork 版本的 WebUI 位于：

```text
pages/bilibot/index.html
pages/bilibot/app.js
pages/bilibot/styles.css
```

插件初始化时会通过 `core/webui_bridge.py` 向 AstrBot 注册页面 API，页面不是独立的公网 Web 服务，也不需要单独启动一个端口。

当前页面围绕以下内容组织：

- **运行状态**：事件状态、失败记录、今日回复、主动行为和数据库状态
- **人格状态**：当前人格状态、关系和行为相关信息
- **配置管理**：读取 `_conf_schema.json` 中的配置项并通过页面保存
- **账号管理**：B 站账号状态、退出登录、二维码生成和扫码轮询
- **主动计划**：每日计划、事件环、主动行为开关和计划重新生成
- **记忆与媒体**：画像、记忆统计、媒体摘要和缓存信息
- **安全中心**：工具分类、能力状态、审计记录和会话隔离信息

首次使用时建议先在 AstrBot 中打开插件页面，确认页面能正常加载，再配置 B 站账号和 LLM。

## 📦 安装

### 方式一：通过 AstrBot 插件管理器

在 AstrBot 插件管理器中搜索 `astrbot_plugin_bilibili_ai_bot` 并安装。安装后重启或重新加载插件。

### 方式二：手动安装 Fork 版本

```bash
git clone https://github.com/zzz27578/astrbot_plugin_bilibili_ai_bot.git
cd astrbot_plugin_bilibili_ai_bot
pip install -r requirements.txt
```

然后将插件目录放入 AstrBot 的插件目录，或使用 AstrBot 支持的本地插件安装方式加载。

如果你需要跟踪本 Fork 的开发分支：

```bash
git clone -b feat/four-layer-refactor https://github.com/zzz27578/astrbot_plugin_bilibili_ai_bot.git
```

### Python 依赖

`requirements.txt` 当前包含：

- `aiohttp`
- `cryptography`
- `lunardate`
- `openai`
- `Pillow`
- `qrcode`
- `yt-dlp`

### 外部命令

主动看视频的视频直读和截帧分析需要系统 PATH 中存在：

- `ffmpeg`
- `ffprobe`

`yt-dlp` 已经是 Python 依赖，不需要再次单独安装。

## ⚙️ 配置

完整配置以仓库中的 [`_conf_schema.json`](_conf_schema.json) 为准。以下是首次启动时最重要的配置：

| 配置项 | 说明 |
| --- | --- |
| `LLM_PROVIDER_ID` | 用于回复和总结的 AstrBot LLM provider ID；留空时按插件实现回退到 AstrBot 默认模型 |
| `SESSDATA`、`BILI_JCT`、`DEDE_USER_ID`、`REFRESH_TOKEN` | B 站登录凭据，可使用 `/bili登录` 扫码自动填入 |
| `OWNER_MID` | 主人的 B 站 UID，用于主人识别、好感度和推荐投递 |
| `OWNER_NAME` | 主人名称，用于提示词和回复上下文 |
| `ENABLE_REPLY` | 是否开启评论回复 |
| `ENABLE_PRIVATE_MESSAGES` | 是否监听和处理 B 站私信 |
| `ENABLE_LIVE_DANMAKU_REPLY` | 是否开启直播弹幕监听与回复 |
| `ENABLE_PROACTIVE` | 是否开启主动看视频 |
| `ENABLE_DYNAMIC` | 是否开启自动发动态 |
| `ENABLE_BANGUMI` | 是否开启番剧相关能力 |
| `EMBED_API_KEY`、`EMBED_API_BASE`、`EMBED_MODEL` | 语义记忆向量化配置 |
| `VIDEO_VISION_PROVIDER_ID` 或视觉 API 配置 | 视频分析配置 |
| `IMAGE_VISION_PROVIDER_ID` 或视觉 API 配置 | 图片识别配置 |
| `IMAGE_GEN_API_KEY`、`IMAGE_GEN_MODEL` | 动态配图配置 |
| `ENABLE_WEB_SEARCH`、`WEB_SEARCH_BACKEND`、`WEB_SEARCH_API_KEY` | 联网搜索配置 |
| `BILI_TOOL_ISOLATION_ENABLED` 等安全配置 | 工具隔离、allowlist、提示词注入防护和审计配置 |
| `AUTONOMOUS_ACTIVITY_LEVEL` 等主动行为配置 | 活跃度、每日上限、最小间隔和固定计划配置 |

### 登录

可以直接发送：

```text
/bili登录
```

按提示扫码，必要时使用：

```text
/bili确认
```

也可以从内置 WebUI 的账号页面生成二维码并轮询登录状态。Cookie 会写入 AstrBot 配置，不要将配置文件、Cookie 或日志中的敏感字段提交到公开仓库。

### 可选能力的退化行为

- 没有 `ffmpeg` / `ffprobe`：视频直读和截帧分析不可用，相关能力会退回到文本分析或跳过。
- 没有视频视觉模型：视频分析能力不可用或退回到文本信息。
- 没有图片识别模型：图片识别会跳过。
- 没有图片生成模型：动态仍可发布，但不能生成 AI 配图。
- 没有联网搜索配置：回复不会调用联网查询。
- 聊天模型不支持 tool calling：LLM 工具不会自动触发，但命令仍可使用。

## 🎮 常用命令

| 命令 | 作用 |
| --- | --- |
| `/bili登录` / `/bili确认` | B 站扫码登录 |
| `/bili状态` | 查看插件、账号和运行状态 |
| `/bili启动` / `/bili停止` | 启动或停止 Bot |
| `/bili开关 <功能>` | 切换私信、直播回复、解析、筛选等功能 |
| `/bili直播 <状态\|房间\|开始\|停止\|测试>` | 管理直播弹幕监听与回复 |
| `/bili计划` | 查看今日主动行为和周期计划 |
| `/bili主动` | 立即触发一次主动看视频 |
| `/bili解析 [链接/BV号]` | 解析 B 站视频或最近引用的视频 |
| `/bili记忆 <关键词>` | 搜索语义记忆 |
| `/bili清算` | 执行记忆清算和整理 |
| `/bili永久记忆` | 查看或管理永久记忆 |
| `/bili好感 [UID]` | 查询好感度 |
| `/bili拉黑 <UID>` / `/bili解黑 <UID>` | 管理黑名单 |
| `/bili黑名单` | 查看黑名单 |
| `/bili性格` / `/bili性格编辑` / `/bili性格删除` | 查看和维护人格演化 |
| `/bili动态` | 手动发布动态 |
| `/bili看番` | 搜索并观看番剧 |
| `/bili番剧记忆` | 查看番剧记忆 |
| `/bili周总结` | 生成本周 B 站生活总结和图片卡片 |
| `/bili绑定 <UID>` / `/bili解绑` | 绑定或解除 QQ 与 B 站 UID |
| `/bili联动` | 查看跨插件记忆接口状态 |
| `/bili帮助` | 查看插件帮助 |

更多命令以当前插件加载结果为准，可使用 `/bili帮助` 查看。

## 🗂️ 数据与文件

插件运行数据默认保存在 AstrBot 的插件数据目录中，路径通常为：

```text
data/plugin_data/astrbot_plugin_bilibili_ai_bot/
```

分层运行时使用独立 SQLite 数据库记录事件、动作、画像、记忆、媒体摘要和审计信息。请定期备份该目录；升级插件时不要直接删除已有数据。

仓库中的：

- `core/storage/schema.sql`：SQLite schema
- `core/webui_bridge.py`：WebUI API bridge
- `pages/bilibot/`：WebUI 页面资源
- `_conf_schema.json`：配置 schema
- `tests/`：自动化测试

## ⚠️ 风险与安全提示

- 插件会使用登录的 B 站账号进行评论、私信、点赞、投币、收藏、关注和发动态等操作，存在账号风控和误操作风险。
- 建议先使用测试账号，并设置合理的轮询间隔、每日上限和主动行为时间。
- 不要把 `SESSDATA`、`BILI_JCT`、`REFRESH_TOKEN`、API Key、SQLite 数据库和完整日志提交到 GitHub。
- 如果启用外部写操作或私信/动态/评论自动发送，请先确认工具 allowlist、身份和能力票据配置。
- 私信和外部输入可能包含恶意链接或提示词注入内容，请保留安全隔离和脱敏配置。
- Fork 版本的 WebUI 应仅在 AstrBot 的受信管理环境中使用，不要自行暴露未经保护的管理接口。

## 🧪 开发与验证

在仓库根目录执行：

```bash
python -m pytest -q
```

当前 Fork 版本本地测试结果：

```text
72 passed
```

修改事件、安全、存储或 WebUI 时，建议至少运行完整测试，并检查 AstrBot 实际加载插件后的日志和页面。

## 🔗 相关链接

- 本 Fork：<https://github.com/zzz27578/astrbot_plugin_bilibili_ai_bot>
- 上游原项目：<https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot>
- AstrBot：<https://github.com/AstrBotDevs/AstrBot>
- AstrBot 文档：<https://docs.astrbot.app/>

## 📄 License

本项目沿用上游仓库的 MIT License。新增代码和文档也在该许可范围内发布；第三方图标素材的授权说明见 [`pages/bilibot/GAME_ICON_PACK_LICENSE.txt`](pages/bilibot/GAME_ICON_PACK_LICENSE.txt)。
