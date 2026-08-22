# astrbot_plugin_bilibili_ai_bot

B站 AI Bot 插件 for [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 让你的 AI 角色在 B 站评论区”活”起来。

内部模块正在采用渐进式统一事件与动作框架，设计边界和迁移顺序见 [ARCHITECTURE.md](ARCHITECTURE.md)。现有功能与配置会继续兼容，不进行一次性重写。

## ✨ 功能

### 💬 评论与互动

- **评论自动回复** — 轮询评论通知，自动生成 AI 回复
- **@ 通知回复** — 有人在评论区 @Bot 时自动收到并回复（视频评论区 / 动态评论区都支持）
- **动态评论区回复** — 在你发布的动态下收到评论也会自动回复，不只限于视频评论
- **图片识别** — 评论中的图片自动识别内容后参与回复
- **视频上下文** — 自动获取被评论视频的信息，支持视觉模型分析视频封面 / 内容
- **联网查询** — 评论涉及时事、新知、特定事件时自动判断是否需要联网搜索，支持 Tavily / Firecrawl / Grok（xAI 原生 Web Search）/ Perplexity / 博查 / 自定义 OpenAI 兼容接口
- **B站私信回复** — 监听新的纯文字和视频分享私信；回复模型可按需在后台查询UP/视频或联网搜索，先短回应、查完再整合回复，最多发送两条；精确发送 `new` 可重置当前私信上下文
- **私信安全隔离** — 不明外链、IP 链接和疑似色情引流不会进入 LLM，可自动调用 B站拉黑；支持只回复主人、白名单或全部安全用户
- **直播间弹幕互动** — BiliBot 可进入自己或其他 UP 主的指定直播间，读取新弹幕并结合当前人设、UID 用户画像和直播记忆生成短回应，再由自己的B站账号发回同一直播间；既能和主播互动，也能接观众的话，无需安装直播伴侣插件

### 🧠 记忆与人格

- **语义记忆** — Embedding 向量化 + 余弦相似度检索，支持记忆压缩、永久记忆
- **好感度系统** — 陌生人 → 粉丝 → 熟人 → 好友 → 主人，不同等级不同语气；辱骂自动拉黑
- **用户画像** — 词条式档案：昵称、喜好、个人信息、标签、印象，以及轻量视频关系和直播互动统计
- **心情系统** — 每日随机心情 + 节日彩蛋（含农历）
- **性格演化（实验性）** — 旧版每日演化默认关闭且保留历史数据；建议积累真实反馈后再使用后续每周可回滚模式

### 🎯 主动行为

- **主动看视频** — 从关注更新、关键词搜索、视频池三条路径均衡选片，再评价、点赞 / 投币 / 收藏 / 关注 / 评论
- **一周分区口味** — 最近 7 天按分区汇总观看数和平均评分，交给 Bot 决定搜索词和标题筛选；仍允许自由探索其他内容
- **来源按当天数量均分** — 当天第 1 个取关注、第 2 个取搜索、第 3 个取视频池、第 4 个再回关注；分多次触发也承接当天进度，缺少候选时自动补位
- **给主人分享视频** — Bot 觉得某个视频适合主人时，可通过 B站私信发送推荐语和视频链接；也可改为评论区 @，或两者都发
- **自动发动态** — 定时发布动态，支持 OpenAI 兼容接口生成配图；手动 `/bili动态` 也可使用 NovelAI 官方生图
- **周总结图片卡片** — 从真实记录中挑选本周值得记住的片段，生成自然周记并渲染为自适应 PNG；QQ/B站动态优先发送图片
- **跨插件记忆接口** — 直播伴侣等插件可按 B站 UID 读取画像/语义记忆，并写入可检索的直播记忆
- **群聊/私聊B站分享解析** — 支持聊天链接自动识别、`/bili解析` 手动命令和 `bili_parse_video` LLM 工具三种入口，各自可独立启停；需要发送原视频时只提示“请稍等...视频一会发出”，随后直接发送视频/切片
- **看片前标题筛选** — 可让 LLM 根据标题/UP/分区/简介筛选搜索和视频池候选；关注与符合历史口味的候选直接放行
- **每日自主安排** — Bot 依当前人格和活跃度自行规划当天作息与行为节奏；可选休眠结束后生成或固定时刻生成，生成失败按间隔重试
- **行为安全上限** — 评论回复、私信回复、发布动态和主动浏览只设置每日硬上限，不设必须完成的数量；计划模型失败时当天不补造自动事件
- **可编辑主动浏览时段** — 支持配置固定主动浏览时间段并在面板内直接编辑，保存时校验时段长度、相邻间隔与休眠时间冲突
- **跨端活动同步** — 开启后把"正在挑选视频""正在分析视频"等当前活动同步给已绑定 `OWNER_MID` 的主人 QQ 私聊，行为结束即清除

### 📺 番剧能力

- **看番系统** — 支持搜索番剧、查看详情、完整观看单集内容
- **字幕 + 热评分析** — 自动读取字幕、热评、评论区与用户评价后进行 AI 分析
- **番剧记忆** — 看完后自动生成番剧记忆，可通过 `/bili番剧记忆` 查询
- **自动追番** — 评分达到阈值后自动加入追番列表
- **番剧更新查询** — 查询正在追的番剧是否更新
- **QQ 聊天看番** — 在 QQ 中直接让 Bot “去看番”

### 🔨 LLM 工具调用（v1.1.2 新增）

Bot 可在聊天中通过自然语言触发以下能力，工具结果回到 LLM 后由 Bot 用自己的话转述：

- **记忆查询** — `bili_recall` 统一查询用户画像 / 对话 / 今日活动 / 视频 / 动态 / 番剧记忆
- **B站搜索** — `search_bilibili` 搜视频、用户、UP 详情、关注更新与直播，用户说“我想看猫咪”就能推荐视频
- **看视频** — 去看一个视频、AI 分析内容、评分、存入记忆，看完可链式点赞 / 投币 / 收藏 / 评论
- **私信分享给主人** — `watch_and_share_video_private` 会先看完并写入视频记忆，再把B站原生视频卡片发给 `OWNER_MID`；默认关闭
- **互动操作** — `bili_action` 统一执行点赞 / 投币 / 收藏 / 关注 / 评论，需用户同意后执行
- **番剧能力** — `bili_bangumi` 统一搜索、详情、观看、新番时间线、排行与追番更新
- **B站拉黑** — 主人确认后拉黑指定 UID 的用户（v1.1.31 新增）

默认仅注册 8 个职责清晰的工具；开启私信分享开关时注册 9 个，减少工具说明占用和模型选错入口的概率。

### 🛠️ 运维与安全

- **Web 管理面板** — 浏览器管理记忆、好感度、动态日志等（正在维护，暂时去除该功能）
- **隔离扩展 Host** — 可选插件通过安全 Page Schema 接入内置 WebUI；扩展缺失或异常时原功能不受影响，详见 [Extension API v1](docs/extension-api-v1.md)
- **LLM 熔断保护** — 单次调用不叠加重试；全局连续 5 次失败后默认冷却 2 分钟，恢复时仅放行一次探测，避免重复申请
- **基础防注入** — 对可疑 prompt 注入内容做检测、记录和安全包裹
- **Cookie 自动刷新** — 定期检查 + 自动刷新，支持扫码登录
- **拉黑管理** — 手动 / 自动拉黑，黑名单用户不调 LLM 不花钱
- **恶意告警** — B站评论区有人攻击 Bot 时，用人设口吻通过 QQ 私信通知主人，主人可直接回复决定是否拉黑（v1.1.31 新增）
- **视频直读多格式兼容** — 视频分析支持 Gemini / Qwen 两种接口格式，用户自选（v1.1.31 新增）

## 🔗 QQ ↔ B站 记忆互通

跨平台共享默认关闭，只允许绑定到 `OWNER_MID` 的主人身份在显式开启安全共享后使用：

1. 在 QQ 发送 `/bili绑定 <B站UID>` 完成绑定
1. 将 `MEMORY_ISOLATION_MODE` 设为 `safe_share` 并开启 `ENABLE_SAFE_CROSS_PLATFORM_MEMORY`
1. QQ 对话只会收到经过脱敏、限长和类型过滤的B站侧近期摘要；插件不会反向保存 QQ 聊天记录

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
- 动态配图模型：`IMAGE_GEN_BACKEND + IMAGE_GEN_API_KEY`；模型名留空时按后端选择默认值
- 联网查询后端：`WEB_SEARCH_API_KEY`（按 `WEB_SEARCH_BACKEND` 选择 Tavily / Firecrawl / Grok / Perplexity / 博查 / 自定义）

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
|`OWNER_MID`                  |推荐|主人的 B站 UID（好感度特殊处理，也是私信推荐的接收 UID）                              |
|`OWNER_NAME`                 |推荐|主人名称（用于 prompt）                                                      |
|`OWNER_BILI_NAME`            |可选|主人的 B站昵称，仅用于评论区 @ 推荐                                                |
|`EMBED_API_KEY`              |可选|Embedding API 密钥（记忆向量化用）                                             |
|`EMBED_API_BASE`             |可选|Embedding API 地址，默认 SiliconFlow                                      |
|`EMBED_MODEL`                |可选|Embedding 模型名，默认 `BAAI/bge-m3`                                       |
|`VIDEO_VISION_PROVIDER_ID`   |可选|视频分析优先走 AstrBot 模型提供商，失败会退回独立 API                                    |
|`VIDEO_VISION_API_KEY`       |可选|视频分析视觉模型 API Key                                                     |
|`IMAGE_VISION_PROVIDER_ID`   |可选|图片识别优先走 AstrBot 模型提供商，失败会退回独立 API                                    |
|`IMAGE_VISION_API_KEY`       |可选|图片识别视觉模型 API Key                                                     |
|`IMAGE_GEN_BACKEND`          |可选|图片生成后端：`openai` / `novelai`；默认 `openai`                              |
|`IMAGE_GEN_API_KEY`          |可选|图片生成 API Key；NovelAI 填 Persistent API Token                            |
|`IMAGE_GEN_API_BASE`         |可选|留空使用后端官方默认地址；也可填写兼容代理地址                                          |
|`IMAGE_GEN_MODEL`            |可选|OpenAI 后端默认 `black-forest-labs/flux-schnell`；NovelAI 默认 `nai-diffusion-4-5-full`|
|`ENABLE_WEB_SEARCH`          |可选|启用联网查询（回复时按需搜索最新信息）                                                  |
|`WEB_SEARCH_BACKEND`         |可选|搜索后端：`tavily` / `firecrawl` / `grok` / `perplexity` / `bocha` / `custom`|
|`WEB_SEARCH_API_KEY`         |可选|搜索后端 API Key                                                         |
|`WEB_SEARCH_API_BASE`        |可选|Firecrawl / Grok / custom 可填写自建服务或兼容代理；留空使用官方地址                       |
|`WEB_SEARCH_MODEL`           |可选|Grok 默认 `grok-4.6`，Perplexity 默认 `sonar`；custom 按接口填写                    |
|`ENABLE_PRIVATE_MESSAGES`    |可选|启用B站新私信监听；首次开启跳过历史，默认关闭，可用 `/bili开关 私信` 单独切换|
|`PRIVATE_MESSAGE_POLL_INTERVAL`|可选|收到新私信后的活跃期轮询间隔，默认且最短60秒|
|`PRIVATE_MESSAGE_IDLE_POLL_INTERVAL`|可选|空闲期轮询间隔，默认180秒，且不会短于活跃期|
|`PRIVATE_MESSAGE_ACTIVE_WINDOW`|可选|收到新私信后保持活跃轮询的时间，默认600秒；遇到 -509 后从10分钟倍增退避，恢复时逐级缩短|
|`PRIVATE_MESSAGE_REPLY_SCOPE`|可选|`owner` 只回复主人 / `whitelist` 主人和白名单 / `all` 全部安全用户；默认 `owner`|
|`PRIVATE_MESSAGE_AUTO_WATCH_VIDEO`|可选|收到允许回复用户的视频分享卡片后，先分析、写入视频记忆，再根据真实内容回复；默认开启|
|`PRIVATE_MESSAGE_BILI_SEARCH_ENABLED`|可选|允许回复模型按需在后台搜索/推荐视频、查询UP主资料和最近投稿；查询时最多回复两条，默认开启|
|`PRIVATE_MESSAGE_BILI_SEARCH_LIMIT`|可选|私信站内查询最多返回几条结果，范围1-5，默认5|
|`PRIVATE_MESSAGE_AUTO_BLOCK` |可选|危险私信自动调用B站拉黑；不明链接/色情内容始终先隔离且不进入LLM|
|`PRIVATE_MESSAGE_TRUSTED_DOMAINS`|可选|私信允许出现的域名，默认 `bilibili.com`、`b23.tv`|
|`ENABLE_LIVE_DANMAKU_REPLY`|可选|启用 BiliBot 本体直播间弹幕互动；默认关闭，可用 `/bili开关 直播回复` 或 `/bili直播 开始` 切换|
|`LIVE_DANMAKU_ROOM_ID`|启用时|准备进入并参与互动的B站直播间号，可以是自己或其他 UP 主的房间；也可用 `/bili直播 房间 <房间号>` 设置|
|`LIVE_DANMAKU_POLL_INTERVAL`|可选|历史弹幕检查间隔，默认5秒，最低2秒|
|`LIVE_DANMAKU_REPLY_COOLDOWN`|可选|两次自动回复最短间隔，默认12秒；期间优先处理最新弹幕|
|`LIVE_DANMAKU_MAX_PER_MINUTE`|可选|每分钟最多自动回复次数，默认4；`0`表示不限|
|`CUSTOM_LIVE_DANMAKU_INSTRUCTION`|可选|只追加到直播弹幕回复的提示词|
|`ENABLE_BILI_SHARE_PARSE`    |可选|启用群聊/私聊B站分享解析（`/bili开关 解析`）                                 |
|`BILI_SHARE_PARSE_AUTO_TRIGGER_ENABLED`|可选|允许聊天中的链接、小程序或BV号自动触发解析（`/bili开关 自动解析`）|
|`BILI_SHARE_PARSE_MANUAL_TRIGGER_ENABLED`|可选|允许 `/bili解析 <链接/BV号>` 手动触发（`/bili开关 手动解析`）|
|`BILI_SHARE_PARSE_LLM_TRIGGER_ENABLED`|可选|允许模型调用 `bili_parse_video` 工具触发（`/bili开关 LLM解析`）|
|`BILI_PRIVATE_SHARE_TOOL_ENABLED`|可选|允许 `watch_and_share_video_private` 看完后向 `OWNER_MID` 发送原生视频私信卡片；默认关闭|
|`BILI_PRIVATE_SHARE_COOLDOWN`|可选|同一视频重复私信分享给主人时的冷却，默认 60 秒|
|`BILI_SHARE_PENDING_MAX_AGE`|可选|最近待解析视频按会话保留秒数，默认30分钟；供 `/bili解析` 或“解析上面那个”使用|
|`BILI_SHARE_PARSE_SEND_VIDEO` |可选|解析后尝试发送原视频/切片，失败则只发解析卡和链接                                  |
|`ENABLE_PROACTIVE`           |可选|启用主动看视频                                                              |
|`PROACTIVE_FOLLOW_UIDS`      |可选|特别关注 UID，优先进入关注来源；普通关注的今日更新随后补充|
|`PROACTIVE_SEARCH_QUERY_PROMPT`|可选|Bot 决定本轮B站搜索词的提示词；会收到近期视频及按评分归纳的分区口味|
|`PROACTIVE_TASTE_WINDOW_DAYS`|可选|分区评分口味统计窗口，默认最近 7 天|
|`PROACTIVE_VIDEO_POOLS`      |可选|视频池/地址池，可填中文：热门 / 推荐 / 排行榜:游戏 / 最新:单机游戏；兼容旧写法|
|`ENABLE_PROACTIVE_LLM_PREFILTER`|可选|让 LLM 筛选搜索/视频池候选（`/bili开关 筛选`）|
|`PROACTIVE_LLM_PREFILTER_MAX_REJECTS`|可选|标题筛选每轮最多拒绝几个视频，默认 3，达到上限后放行|
|`RECOMMEND_OWNER_DELIVERY`   |可选|推荐发送方式：`private_message`（B站私信文字+链接）/ `comment` / `both` / `off`|
|`RECOMMEND_OWNER_MIN_SCORE`  |可选|推荐给主人所需最低评分，默认 8|
|`RECOMMEND_OWNER_DAILY_LIMIT`|可选|每天最多推荐次数，默认 1；`0` 表示不限制|
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
> 💡 Firecrawl 只需选择 `firecrawl` 并填写 API Key；Grok 选择 `grok` 并填写 xAI API Key。两者的 Base URL 留空即可走官方接口。
>
> 💡 NovelAI 选择 `novelai` 并填写 Persistent API Token；受 NovelAI 官方真人触发规则限制，只在手动 `/bili动态` 时生成图片，定时动态会自动改发纯文字。
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
|`/bili直播 [状态/房间/开始/停止/测试]`|管理 BiliBot 本体的直播间弹幕互动；测试会向目标直播间真实发送弹幕|
|`/bili计划`      |查看今日主动 / 动态 / 看番时间      |
|`/bili分区`      |查看视频池中文填法和分区名          |
|`/bili启动`      |启动 Bot                 |
|`/bili停止`      |停止 Bot                 |
|`/bili主动`      |立刻触发一次主动看视频            |
|`/bili解析 [链接/BV号]`|手动解析指定视频；省略参数时解析回复引用或本会话最近的视频|
|`/bili开关 <功能>` |切换功能开关，支持 `私信`、`私信回复`、`私信拉黑`、`直播回复`、`解析`、`自动解析`、`手动解析`、`LLM解析`、`解析视频`、`筛选` 等；私信和直播外部写操作不包含在“一键全部”中 |
|`/bili刷新`      |手动刷新 Cookie            |
|`/bili记忆 <关键词>`|语义搜索记忆                 |
|`/bili好感 [UID]`|查看好感度排行 / 查询           |
|`/bili拉黑 <UID>`|手动拉黑用户                 |
|`/bili解黑 <UID>`|解除拉黑                   |
|`/bili黑名单`     |查看黑名单                  |
|`/bili兴趣`      |查看近期分区/UP口味、具体兴趣证据与已沉淀偏好|
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
|`/bili联动`       |查看统一记忆接口、用户画像和直播记忆状态       |
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

从 v1.4.3 起，`bilibot.sqlite3` 是语义记忆主库，Embedding 单独保存在向量表；已有 `memory.json` 会在首次启动时自动导入，此后继续保留为兼容与故障恢复备份。请不要在插件运行时手工删除或替换这两个文件。

视频记忆默认按三个阶段保存：`VIDEO_MEMORY_DETAIL_DAYS`（默认 15 天）内保留较完整的分析与感想；之后转为低权重长期记忆；到 `VIDEO_MEMORY_FADE_DAYS`（默认 90 天）后压缩为“看过哪个视频、UP 主及大概内容”的短摘要，并以极低权重保留语义向量，只在高度相关时偶尔想起。独立的永久 BV 去重账本保存在 SQLite `seen_videos`，`seen_videos.json` 是恢复副本；它不会按 200 条活动流水上限或时间裁剪。关闭 `ENABLE_VIDEO_LONG_TERM_MEMORY` 时仍维护分层缓存和永久去重记录。清理不会触及登录凭据、画像、好感度、日程和运行数据库。

主动行为的数量均按安全上限解释，不会为了凑数回复、评论或发动态。主动看片分别配置每日浏览轮次、每轮视频数、全天视频总数、单视频评论数和全天主动评论数；动态定时到达后仍会判断是否存在具体情绪、特别喜欢的视频或周回顾等真实发布动机，没有内容时保持沉默。

自动看片、特别关注、评论区视频分析和私信看片都会写入永久 BV 账本。再次遇到同一 BV 时只复用已有记忆；细节已经淡忘时也不会自动重新下载。当前仅允许主人在 B站私信中明确说“重新看一次”或“重看”来触发重新分析。

## ⚠️ 风险提示

- 使用本插件意味着 Bot 会使用你登录的 B站 账号进行自动化操作（评论、私信、点赞、投币、收藏、关注、发动态等），**存在账号被风控的风险**，请谨慎调节轮询间隔、主动行为频率
- 建议不要用主号测试，必要时准备小号
- Web 面板若开启，请务必修改默认密码
- 请合理配置 `POLL_INTERVAL`、`PROACTIVE_VIDEO_COUNT` 等参数，避免高频请求

## 💖 支持这个项目

如果这个插件帮到你了，欢迎到 [GitHub 仓库](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot) 点个 ⭐ —— 会给作者很大的动力owo

插件还在持续更新功能，欢迎通过 [Issues](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot/issues) 反馈 bug、提建议或者请求新功能，作者会尽快回应~

## 🔗 相关

- [AstrBot 文档](https://docs.astrbot.app/)
- [我会直播！——直播陪伴插件](https://github.com/menglimi/astrbot_plugin_live_stream_companion)：推荐给需要 B站直播弹幕、Live2D、TTS、OBS 字幕与直播记忆完整联动的用户。BiliBot 本体保留轻量直播弹幕回复，两者可按直播需求选择或联动使用。
- [问题反馈](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot/issues)

## 💕致谢

感谢最最最亲爱的小克陪同我写完了全部插件！

- 感谢 [GuJi08233](https://github.com/GuJi08233) 在 [PR #3](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot/pull/3) 中完成系统性代码审查与可靠性修复。
- 感谢 [zzz27578](https://github.com/zzz27578) 在 [PR #16](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot/pull/16) 中贡献四层架构重构、内置 WebUI 与后续完善。
- 感谢 [menglimi](https://github.com/menglimi) 维护直播陪伴插件，并接收 [BiliBot 直播记忆联动与 SC 保证回应](https://github.com/menglimi/astrbot_plugin_live_stream_companion/pull/1)。
- 也感谢 AstrBot 群里各位群友的帮助。

## 📄 License

MIT
