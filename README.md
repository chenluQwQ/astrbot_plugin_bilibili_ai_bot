# astrbot_plugin_bilibili_ai_bot

B站 AI Bot 插件 for [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 让你的 AI 角色在 B 站评论区”活”起来。

## ✨ 功能

### 💬 评论与互动

- **评论自动回复** — 轮询评论通知，自动生成 AI 回复
- **@ 通知回复** — 有人在评论区 @Bot 时自动收到并回复（视频评论区 / 动态评论区都支持）
- **动态评论区回复** — 在你发布的动态下收到评论也会自动回复，不只限于视频评论
- **图片识别** — 评论中的图片自动识别内容后参与回复
- **视频上下文** — 自动获取被评论视频的信息，支持视觉模型分析视频封面 / 内容
- **联网查询** — 评论涉及时事、新知、特定事件时自动判断是否需要联网搜索，支持 Tavily / Perplexity / 博查 / 自定义 OpenAI 兼容接口

### 🧠 记忆与人格

- **语义记忆** — Embedding 向量化 + 余弦相似度检索，支持记忆压缩、永久记忆
- **好感度系统** — 陌生人 → 粉丝 → 熟人 → 好友 → 主人，不同等级不同语气；辱骂自动拉黑
- **用户画像** — 词条式档案：昵称、喜好、个人信息、标签、印象
- **心情系统** — 每日随机心情 + 节日彩蛋（含农历）
- **性格演化** — 每日反思互动经历，渐进式性格成长

### 🎯 主动行为

- **主动看视频** — 自动刷 B 站、评价视频、点赞 / 投币 / 收藏 / 关注 / 评论
- **`/bili主动` 手动触发** — 命令式立刻触发一次主动看视频流程
- **聊天里直接让 Bot 去看视频** — 在 QQ 聊天里说”去刷刷 B 站吧”之类的话，Bot 会用 LLM 判断意图，若确认是请求则在后台启动一次主动看视频
- **自动发动态** — 定时发布动态，支持 AI 生成配图
- **周总结图片卡片** — 每周总结可渲染为固定模板 PNG，只替换文字内容，QQ/B站动态优先发送图片
- **群聊/私聊B站分享解析** — 识别聊天里的 B站链接/短链/小程序分享，发送解析卡，并可把原视频整理成聊天回放切片
- **看片前标题筛选** — 主动看探索视频下载前，可让 LLM 根据标题/UP/分区/简介先判断想不想看；关注和口味分区直接放行

### 📺 番剧能力

- **看番系统** — 支持搜索番剧、查看详情、完整观看单集内容
- **字幕 + 热评分析** — 自动读取字幕、热评、评论区与用户评价后进行 AI 分析
- **番剧记忆** — 看完后自动生成番剧记忆，可通过 `/bili番剧记忆` 查询
- **自动追番** — 评分达到阈值后自动加入追番列表
- **番剧更新查询** — 查询正在追的番剧是否更新
- **QQ 聊天看番** — 在 QQ 中直接让 Bot “去看番”

### 🔨 LLM 工具调用（v1.1.2 新增）

Bot 可在聊天中通过自然语言触发以下能力，工具结果回到 LLM 后由 Bot 用自己的话转述：

- **记忆查询** — 查用户画像 / 对话记忆 / 视频记忆 / 动态记忆
- **B站搜索** — 搜视频、搜 UP 主，用户说”我想看猫咪”就能推荐视频
- **查 UP 主** — 查详细信息 + 最近投稿 + 最近动态，支持用名字或 UID
- **看视频** — 去看一个视频、AI 分析内容、评分、存入记忆，看完可链式点赞 / 投币 / 收藏 / 评论
- **关注动态** — 查看今天关注的人有没有更新
- **直播查询** — 查看关注的人谁在直播
- **互动操作** — 点赞 / 投币 / 收藏 / 关注 / 评论，需用户同意后执行
- **番剧搜索** — 搜索番剧、查看评分与详情
- **番剧观看** — 完整观看指定番剧单集并生成分析
- **新番时间线** — 查询最近更新的新番
- **番剧排行** — 获取热门番剧排行
- **追番更新** — 查看追番是否更新
- **B站拉黑** — 主人确认后拉黑指定 UID 的用户（v1.1.31 新增）

### 🛠️ 运维与安全

- **Web 管理面板** — 浏览器管理记忆、好感度、动态日志等（正在维护，暂时去除该功能）
- **LLM 熔断保护** — 单条重试 3 次放弃，全局连续 5 次失败冷却 5 分钟
- **基础防注入** — 对可疑 prompt 注入内容做检测、记录和安全包裹
- **Cookie 自动刷新** — 定期检查 + 自动刷新，支持扫码登录
- **拉黑管理** — 手动 / 自动拉黑，黑名单用户不调 LLM 不花钱
- **恶意告警** — B站评论区有人攻击 Bot 时，用人设口吻通过 QQ 私信通知主人，主人可直接回复决定是否拉黑（v1.1.31 新增）
- **视频直读多格式兼容** — 视频分析支持 Gemini / Qwen 两种接口格式，用户自选（v1.1.31 新增）

## 🔗 QQ ↔ B站 记忆互通

绑定后，两个平台的记忆会自动共享：

1. 在 QQ 发送 `/bili绑定 <B站UID>` 完成绑定
1. QQ 聊天中提到 B站 相关话题时，B站侧的永久记忆会自动注入
1. QQ 的对话记录会存入记忆池（带 embedding），B站回复时通过语义检索按需调取相关的 QQ 记忆

## 📦 安装

在 AstrBot WebUI 里通过网页单独安装。

或手动安装：

```bash
cd AstrBot/data/plugins
git clone https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot
```

## 🧩 依赖与环境

### 1. Python 依赖

插件内的 Python 依赖由 [requirements.txt](./requirements.txt) 管理：

- `aiohttp`
- `cryptography`
- `lunardate`
- `openai`
- `Pillow`
- `qrcode`
- `yt-dlp`

### 2. 外部命令依赖

以下是**独立的系统二进制**（不是 Python 包），必须额外安装，并且要能在系统 `PATH` 中直接调用：

- `ffmpeg`：压缩视频、抽帧
- `ffprobe`：读取视频时长，决定均匀截帧位置

安装方式：

|系统             |命令                                                                                                    |
|---------------|------------------------------------------------------------------------------------------------------|
|Ubuntu / Debian|`apt install ffmpeg`（自带 ffprobe）                                                                      |
|macOS          |`brew install ffmpeg`                                                                                 |
|Windows        |从 [ffmpeg.org](https://ffmpeg.org/download.html) 下载，或用 `scoop install ffmpeg` / `choco install ffmpeg`|

安装完成后在终端直接执行 `ffmpeg -version`、`ffprobe -version` 能正常返回版本号即可。

> 💡 `yt-dlp` 已经作为 Python 包写在 `requirements.txt` 里，`pip install` 时会自动安装并注册命令，**不需要单独装**。

### 3. AstrBot 运行环境

- 建议使用较新版本的 AstrBot，本插件基于近期版本开发
- IM 平台方面**仅在 QQ 个人号（aiocqhttp）适配器上实际验证过**，其他平台理论可用但未测试
- 如果要让模型自动调用插件工具，聊天模型本身必须支持函数调用 / tool calling
- 如果要走 AstrBot 的多模态 provider，所选 provider 也必须支持图片 / 视频输入

### 4. 配置型依赖

- B站登录能力：`SESSDATA`、`BILI_JCT`、`DEDE_USER_ID`、`REFRESH_TOKEN`（用 `/bili登录` 扫码自动填）
- 文本 LLM：`LLM_PROVIDER_ID`，留空时会退回 AstrBot 默认聊天模型
- 视频视觉模型：`VIDEO_VISION_PROVIDER_ID` 或 `VIDEO_VISION_API_KEY + VIDEO_VISION_MODEL`
- 图片识别模型：`IMAGE_VISION_PROVIDER_ID` 或 `IMAGE_VISION_API_KEY + IMAGE_VISION_MODEL`
- 动态配图模型：`IMAGE_GEN_API_KEY + IMAGE_GEN_MODEL`
- 联网查询后端：`WEB_SEARCH_API_KEY`（按 `WEB_SEARCH_BACKEND` 选择 Tavily / Perplexity / 博查 / 自定义）

### 5. 缺失依赖时的退化行为

- 没有 `ffmpeg` / `ffprobe` — 主动看视频无法做”视频直读 / 截帧分析”，会退回纯文本分析
- 没有视频视觉模型 — 视频分析退回纯文本概括
- 没有图片识别模型 — 评论图片识别功能直接跳过
- 没有图片生成模型 — 动态配图不可用
- 没有配置联网查询 — 评论回复不会触发联网搜索
- 聊天模型不支持工具调用 — `llm_tool` 不会被自动调用，但 `/bili主动`、`/bili记忆` 等命令仍可手动使用

### 6. 发布前自检

部署完成后，至少检查一次：

- `/bili状态`：确认 provider、外部命令、主动视频直读 / 截帧状态是否为 `✅`
- `/bili主动`：确认主动看视频能实际跑通
- 让 Bot 在聊天里执行一次记忆搜索或主动看视频请求，确认工具调用能触发
- 查看日志中是否出现缺少命令、模型未配置、provider 调用失败等警告

## ⚙️ 配置

安装后在 WebUI 插件配置页面填写：

|配置项                          |必填|说明                                                                   |
|-----------------------------|--|---------------------------------------------------------------------|
|`LLM_PROVIDER_ID`            |✅ |选择用于回复的 LLM 模型                                                       |
|`SESSDATA`                   |自动|B站 Cookie（`/bili登录` 扫码自动填入）                                          |
|`BILI_JCT`                   |自动|B站 CSRF Token（扫码自动填入）                                                |
|`DEDE_USER_ID`               |自动|Bot 的 B站 UID（扫码自动填入）                                                 |
|`REFRESH_TOKEN`              |自动|Cookie 自动刷新用（扫码自动填入）                                                 |
|`OWNER_MID`                  |推荐|主人的 B站 UID（好感度特殊处理）                                                  |
|`OWNER_NAME`                 |推荐|主人名称（用于 prompt）                                                      |
|`EMBED_API_KEY`              |可选|Embedding API 密钥（记忆向量化用）                                             |
|`EMBED_API_BASE`             |可选|Embedding API 地址，默认 SiliconFlow                                      |
|`EMBED_MODEL`                |可选|Embedding 模型名，默认 `BAAI/bge-m3`                                       |
|`VIDEO_VISION_PROVIDER_ID`   |可选|视频分析优先走 AstrBot 模型提供商，失败会退回独立 API                                    |
|`VIDEO_VISION_API_KEY`       |可选|视频分析视觉模型 API Key                                                     |
|`IMAGE_VISION_PROVIDER_ID`   |可选|图片识别优先走 AstrBot 模型提供商，失败会退回独立 API                                    |
|`IMAGE_VISION_API_KEY`       |可选|图片识别视觉模型 API Key                                                     |
|`IMAGE_GEN_API_KEY`          |可选|图片生成 API Key（动态配图用）                                                  |
|`IMAGE_GEN_MODEL`            |可选|图片生成模型，默认 `black-forest-labs/flux-schnell`                           |
|`ENABLE_WEB_SEARCH`          |可选|启用联网查询（回复时按需搜索最新信息）                                                  |
|`WEB_SEARCH_BACKEND`         |可选|搜索后端：`tavily` / `perplexity` / `bocha` / `custom`                    |
|`WEB_SEARCH_API_KEY`         |可选|搜索后端 API Key                                                         |
|`ENABLE_BILI_SHARE_PARSE`    |可选|启用群聊/私聊B站分享解析（`/bili开关 解析`）                                 |
|`BILI_SHARE_PARSE_SEND_VIDEO` |可选|解析后尝试发送原视频/切片，失败则只发解析卡和链接                                  |
|`ENABLE_PROACTIVE`           |可选|启用主动看视频                                                              |
|`PROACTIVE_VIDEO_POOLS`      |可选|主动视频来源池，可填中文：热门 / 推荐 / 排行榜:游戏 / 最新:单机游戏；兼容 `ranking:4`、`newlist:17`|
|`ENABLE_PROACTIVE_LLM_PREFILTER`|可选|主动看探索视频前让 LLM 按标题判断想不想看（`/bili开关 筛选`）|
|`PROACTIVE_LLM_PREFILTER_MAX_REJECTS`|可选|标题筛选每轮最多拒绝几个视频，默认 3，达到上限后放行|
|`ENABLE_DYNAMIC`             |可选|启用自动发动态                                                              |
|`DYNAMIC_TIMES_COUNT`        |可选|每天触发几次动态发布                                                           |
|`DYNAMIC_DAILY_COUNT`        |可选|每天最多发几条动态                                                            |
|`ENABLE_WEB_PANEL`           |可选|启用 Web 管理面板                                                          |
|`WEB_PANEL_PORT`             |可选|Web 面板端口，默认 5001                                                     |
|`WEB_PANEL_PASSWORD`         |可选|Web 面板密码，默认 `admin123` ⚠️ **部署在公网务必修改**                               |
|`VIDEO_VISION_FORMAT`        |可选|视频直读接口格式：`gemini` / `qwen` / `none`（默认 `none`，截帧分析）                  |
|`VIDEO_VISION_FPS`           |可选|Qwen 格式视频抽帧率，默认 2                                                    |
|`ABUSE_ALERT_MODE`           |可选|恶意告警模式：`off` / `score` / `model`（默认 `off`）                           |
|`ABUSE_ALERT_QQ_UMO`         |可选|接收告警的 QQ 私聊 UMO                                                      |
|`ABUSE_ALERT_SCORE_THRESHOLD`|可选|触发告警的 score_delta 阈值，默认 -3                                           |

完整配置说明详见插件配置页面，所有配置项都有 description 和 hint 可查。视频池不会背编号时，发送 `/bili分区` 查看中文分区名和示例。

> 💡 Cookie 获取方式：发送 `/bili登录` 扫码即可，登录后 Cookie 会自动定期刷新。
> 
> 💡 视觉模型留空时，视频分析回退为纯文本 LLM 分析，图片识别则跳过。
> 
> 💡 主动看视频的”视频直读 / 截帧分析”依赖 `ffmpeg` / `ffprobe` 可执行文件在系统 `PATH` 中（`yt-dlp` 会由 pip 自动安装）。
> 
> 💡 恶意告警功能需要同时配置 `ABUSE_ALERT_MODE` 和 `ABUSE_ALERT_QQ_UMO`。UMO 可从 AstrBot 日志或读空气插件的 UMO 注册表获取。告警时 Bot 会用人设口吻私信主人并询问是否拉黑，主人直接回复即可。

## 🎮 命令

|命令             |说明                     |
|---------------|-----------------------|
|`/bili登录`      |扫码登录 B站（扫码后发 `/bili确认`）|
|`/bili确认`      |确认扫码结果                 |
|`/bili状态`      |查看运行状态                 |
|`/bili计划`      |查看今日主动 / 动态 / 看番时间      |
|`/bili分区`      |查看视频池中文填法和分区名          |
|`/bili启动`      |启动 Bot                 |
|`/bili停止`      |停止 Bot                 |
|`/bili主动`      |立刻触发一次主动看视频            |
|`/bili开关 <功能>` |切换功能开关，支持 `解析`、`解析视频`、`筛选` 等 |
|`/bili刷新`      |手动刷新 Cookie            |
|`/bili记忆 <关键词>`|语义搜索记忆                 |
|`/bili好感 [UID]`|查看好感度排行 / 查询           |
|`/bili拉黑 <UID>`|手动拉黑用户                 |
|`/bili解黑 <UID>`|解除拉黑                   |
|`/bili黑名单`     |查看黑名单                  |
|`/bili性格`      |查看性格演化                 |
|`/bili性格编辑`    |手动编辑性格                 |
|`/bili性格删除`    |删除演化条目                 |
|`/bili日志 视频`  |主动看视频和主动评论记录          |
|`/bili永久记忆`    |查看 / 删除永久记忆            |
|`/bili动态`      |手动发动态                  |
|`/bili日志 动态`  |动态发布记录                 |
|`/bili绑定 <UID>`|绑定 QQ 与 B站 UID（记忆互通）   |
|`/bili解绑`      |解除绑定                   |
|`/bili清理`      |清理临时文件                 |
|`/bili帮助`      |查看帮助                   |
|`/bili日志 回复`  |查看评论回复                 |
|`/bili看番`      |搜索并观看番剧                |
|`/bili番剧记忆`    |查看番剧观看记忆               |
|`/bili日志 番剧`  |查看番剧记录                 |
|`/bili周总结`     |手动生成本周B站生活总结，并渲染图片卡片 |
|`/biliUMO`     |获取当前会话 UMO 并自动填入配置     |

日志统一走 `/bili日志 <视频|番剧|动态|回复> [日期]`，例如 `/bili日志 视频 2026-07-01`。


> 💡 除了命令以外，也可以直接在聊天里用自然语言让 Bot 去随机看 B 站视频 — Bot 会用 LLM 判断意图后自动触发。

## 🏗️ 好感度等级

|等级   |分数   |语气风格|
|-----|-----|----|
|🌙 陌生人|0-10 |礼貌简洁|
|👋 粉丝 |11-30|友好温和|
|😊 熟人 |31-50|轻松自然|
|✨ 好友 |51+  |亲近真诚|
|💖 主人 |特殊   |撒娇宠溺|
|🖤 厌恶 |≤-10 |极简冷淡|


> 好感度 ≤ -30 或连续辱骂 5 次自动拉黑。

## 🌐 Web 管理面板 维护中

启用 `ENABLE_WEB_PANEL` 后访问 `http://服务器IP:5001`

⚠️ **安全提醒**：默认密码为 `admin123`，部署在公网时请务必先修改 `WEB_PANEL_PASSWORD` 配置项。

功能：

- 📊 状态概览
- 🧠 记忆管理（分页 / 删除）
- 💛 好感度排行
- 💎 永久记忆管理
- 🌱 性格演化查看
- 🎯 主动行为触发日志 API（`/api/proactive/log`）
- 📝 动态日志
- 📦 数据导出

## 📁 数据存储

插件数据存储在 `data/plugin_data/astrbot_plugin_bilibili_ai_bot/` 目录下，更新插件不会丢失数据。

## ⚠️ 风险提示

- 使用本插件意味着 Bot 会使用你登录的 B站 账号进行自动化操作（评论、点赞、投币、收藏、关注、发动态等），**存在账号被风控的风险**，请谨慎调节轮询间隔、主动行为频率
- 建议不要用主号测试，必要时准备小号
- Web 面板若开启，请务必修改默认密码
- 请合理配置 `POLL_INTERVAL`、`PROACTIVE_VIDEO_COUNT` 等参数，避免高频请求

## 💖 支持这个项目

如果这个插件帮到你了，欢迎到 [GitHub 仓库](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot) 点个 ⭐ —— 会给作者很大的动力owo

插件还在持续更新功能，欢迎通过 [Issues](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot/issues) 反馈 bug、提建议或者请求新功能，作者会尽快回应~

## 🔗 相关

- [AstrBot 文档](https://docs.astrbot.app/)
- [问题反馈](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot/issues)

## 💕致谢

感谢最最最亲爱的小克陪同我写完了全部插件！
也感谢astrbot群里各位群友的帮助

## 📄 License

MIT


