# astrbot_plugin_bilibili_ai_bot

B站 AI Bot 插件 for [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 让你的 AI 角色在 B 站评论区”活”起来。

Bot 有自己的一天：睡醒、排日程、刷视频、回评论和私信、发动态，夜里整理记忆。看视频时看画面也听声音，记得看过什么、和谁聊过什么。

## 📦 安装

在 AstrBot 后台通过网页安装，或者手动安装：

```bash
cd AstrBot/data/plugins
git clone https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot
```

Python 依赖会按 [requirements.txt](./requirements.txt) 自动装。另外要装 **ffmpeg**（自带 ffprobe），看视频要用：

|系统|命令|
|---|---|
|Ubuntu / Debian|`apt install ffmpeg`|
|macOS|`brew install ffmpeg`|
|Windows|从 [ffmpeg.org](https://ffmpeg.org/download.html) 下载，或 `scoop install ffmpeg` / `choco install ffmpeg`|

装好后终端里 `ffmpeg -version`、`ffprobe -version` 能返回版本号就行。

## 🚀 第一次配置

1. **登录 B站**：在 QQ 里发 `/bili登录`，用 Bot 的 B站账号扫码，再发 `/bili确认`。Cookie 会自动填好、定期刷新。建议用小号，别用主号
2. **填主人**：在管理页「账号」里填主人的 B站 UID（`OWNER_MID`）和称呼（`OWNER_NAME`）
3. **选模型**：在「基础设置」里选
   - 回复用的模型（`LLM_PROVIDER_ID`），留空用 AstrBot 默认模型
   - 视频视觉模型：能看视频的模型可以直接看视频、听声音；不能的话改看截图
   - 记忆向量：可以直接填 AstrBot 里配好的向量模型 ID（`EMBED_PROVIDER_ID`），也可以单独填 API Key

填完发 `/bili状态`，模型、ffmpeg、视频分析那几项都是 ✅ 就可以了；再发一次 `/bili主动`，看能不能正常看完一个视频。

管理页里新手引导会带着走一遍。其余配置都在管理页，每项都有说明。

## ✨ 功能

**评论和私信**
- 回复视频、动态评论区的评论和 @；评论里的图片会先看一遍
- 回复前可以自己查资料：简介、字幕、热评、联网搜索，也能带着问题回头看视频的某一段（需要聊天模型支持工具调用）
- B站私信：可以只回主人、白名单或所有人；不明链接、引流私信不交给模型，可以自动拉黑
- 好感度从陌生人到好友，不同熟悉程度说话不一样；辱骂多了自动拉黑

**主动看视频**
- 从关注更新、关键词搜索、视频池里挑视频，按最近一周的口味选，也会看点别的
- 看画面也听声音，按时间记下看到、听到的；评论落在具体的一段、一句话上
- 看完自己决定点赞、投币、收藏、评论；碰到觉得主人会喜欢的，私信分享给主人

**动态和番剧**
- 发动态：日常、吐槽、看过的视频和番，可以配图；不为了凑数发
- 追番：看字幕、热评，写观后感，评分高的加入追番
- 周总结：从这一周的记录里挑值得记的，写成周记，渲染成图片

**记忆**
- 记得看过的视频、聊过的天、每个人的画像；记忆慢慢变淡，重要的留得久
- Bot 关于自己和主人有最多 8 条永久记忆，可以自己改
- 别人推荐的视频、番剧会记进备忘录，之后排进日程去看

**QQ 里**
- 群聊、私聊里分享的 B站链接会自动解析，Bot 自己决定看不看
- 可以用自然语言让 Bot 去看视频、查 UP、看番（工具默认关闭，见下面常见问题）
- 主人可以把 QQ 和 B站的记忆打通（默认关闭）

**管理页**：在 AstrBot 后台打开本插件的页面就是，不用另开端口。能看 Bot 现在在干嘛、今天看了什么写了什么（往前能翻 30 天）、日程、记忆和好感度，所有配置也在这里改。

## 🎮 命令

普通用户能用 `/bili分区`、`/bili解析`、`/bili绑定`、`/bili解绑`、`/bili帮助`，其他命令需要 AstrBot 管理员。

|命令|说明|
|---|---|
|`/bili登录`、`/bili确认`|扫码登录 B站|
|`/bili刷新`|手动刷新 Cookie|
|`/bili状态`|运行状态、模型和依赖检查|
|`/bili启动`、`/bili停止`|启动、停止 Bot|
|`/bili开关 <功能>`|切换功能开关，比如 `私信`、`私信回复`、`解析`、`筛选`、`查询`|
|`/bili计划`|今天的日程|
|`/bili主动`|马上看一轮视频|
|`/bili动态`|马上发一条动态|
|`/bili看番`|搜索并观看番剧|
|`/bili周总结`|生成本周总结图片|
|`/bili解析 [链接/BV号]`|解析视频；不带参数时解析引用或最近的视频|
|`/bili分区`|视频池的分区名和填法|
|`/bili日志 <视频\|番剧\|动态\|回复\|查询> [日期]`|查看记录，日期格式 `2026-07-01`|
|`/bili记忆 <关键词>`|搜索记忆|
|`/bili永久记忆`|查看、删除永久记忆|
|`/bili番剧记忆`|看过的番|
|`/bili备忘录`|查看、删除备忘录|
|`/bili兴趣`|最近的口味和偏好|
|`/bili好感 [UID]`|好感度排行或查询|
|`/bili拉黑 <UID>`、`/bili解黑 <UID>`、`/bili黑名单`|黑名单|
|`/bili性格`、`/bili性格编辑`、`/bili性格删除`、`/bili性格回滚`|性格演化（实验性）|
|`/bili绑定 <UID>`、`/bili解绑`|绑定 QQ 和 B站 UID|
|`/bili联动`|给其他插件用的记忆接口状态|
|`/biliUMO`|获取当前会话 UMO 并填入配置|
|`/bili测试查询 BV号 [问题]`|试一次评论回复前查资料，不发评论|
|`/bili清算`|手动触发夜里的记忆整理|
|`/bili清理`|清理临时文件|
|`/bili清理老化`|删掉已过保留期的长期记忆|
|`/bili清理旧`|把已导入数据库的旧 json 挪进 `old_json/`|
|`/bili导出`|把数据库导出成旧版 json，退回旧版本或备份时用|
|`/bili迁移记忆`|给没有分级的旧记忆补上分级|
|`/bili帮助`|帮助|

## ❓ 常见问题

**缺了某个依赖会怎样？**
- 没有 ffmpeg：不能看视频，只看标题、简介
- 没有视频视觉模型：视频只按文字概括
- 没有图片识别模型：评论里的图片跳过
- 没开联网搜索：回复时不联网查
- 聊天模型不支持工具调用：回复前不查资料，QQ 里的自然语言工具也用不了，命令照常能用

**怎么让 Bot 在 QQ 里用工具（看视频、查 UP、点赞评论）？**
打开 `ENABLE_LLM_TOOLS`。默认只有 AstrBot 管理员能让 Bot 调工具（在任意平台发 `/sid` 查自己的 ID，填进 AstrBot 的管理员 ID）。B站评论和私信里的人永远不算管理员。
- 让其他人也能查视频、查 UP：开 `SECURITY_ALLOW_NON_ADMIN_READ_TOOLS`
- 让其他人也能请 Bot 去看视频：开 `SECURITY_ALLOW_NON_ADMIN_WATCH`，去不去 Bot 自己定
- 查私密数据（关注更新、追番）：开 `SECURITY_ENABLE_PRIVATE_READ_TOOLS`
- 点赞、投币、评论、拉黑这些写操作：开 `SECURITY_ENABLE_WRITE_TOOLS`，只有管理员能用；想每次先确认，再开 `SECURITY_WRITE_REQUIRE_CONFIRM`

**要走代理怎么填？**
在管理页「基础设置 → 网络代理」填 `PROXY_URL`，比如 `http://127.0.0.1:7890` 或 `socks5://127.0.0.1:1080`。填代理服务地址，不是订阅链接。只影响本插件的请求，不改系统和 AstrBot 的设置。Docker 里 `127.0.0.1` 是容器自己，代理在宿主机上要填容器能访问到的宿主机地址。

**数据存在哪？**
`data/plugin_data/astrbot_plugin_bilibili_ai_bot/`，更新插件不会丢。主要数据在 `bilibot.sqlite3` 里，插件运行时别手动删或替换它。

**恶意告警怎么配？**
同时填 `ABUSE_ALERT_MODE` 和 `ABUSE_ALERT_QQ_UMO`（在要接收的 QQ 私聊里发 `/biliUMO` 自动填）。有人在评论区攻击 Bot 时，Bot 会私信主人，主人回复决定拉不拉黑。

**只测过哪些平台？**
QQ 个人号（aiocqhttp）实际验证过，其他 AstrBot 平台理论上能用，没测过。

## ⚠️ 风险提示

- Bot 会用你登录的 B站账号自动评论、私信、点赞、投币、关注、发动态，**有被风控的风险**
- 建议用小号，不要用主号测试
- 轮询间隔、每天看几个视频这些别调得太频繁

## 💖 支持这个项目

如果这个插件帮到你了，欢迎到 [GitHub 仓库](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot) 点个 ⭐ —— 会给作者很大的动力owo

插件还在持续更新功能，欢迎通过 [Issues](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot/issues) 反馈 bug、提建议或者请求新功能，作者会尽快回应~

想提 PR 的话先看 [维护说明](CONTRIBUTING.md)：现在在做什么、代码怎么分、几条必须守的规矩。更新记录见 [CHANGELOG.md](CHANGELOG.md)。

## 🔗 相关

- [AstrBot 文档](https://docs.astrbot.app/)
- [我会直播！——直播陪伴插件](https://github.com/menglimi/astrbot_plugin_live_stream_companion)：推荐给需要 B站直播弹幕、Live2D、TTS、OBS 字幕与直播记忆完整联动的用户。BiliBot 本体保留轻量直播弹幕回复，两者可按直播需求选择或联动使用。
- 给其他插件用的扩展接口：[Extension API v1](docs/extension-api-v1.md)

## 💕感谢重要的人

感谢最最最亲爱的小克陪同我写完了全部插件！

- 感谢 [GuJi08233](https://github.com/GuJi08233) 在 [PR #3](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot/pull/3) 中完成系统性代码审查与可靠性修复。
- 感谢 [zzz27578](https://github.com/zzz27578) 在 [PR #16](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot/pull/16) 中贡献四层架构重构、内置 WebUI 与后续完善。
- 感谢 [menglimi](https://github.com/menglimi) 维护直播陪伴插件，并接收 [BiliBot 直播记忆联动与 SC 保证回应](https://github.com/menglimi/astrbot_plugin_live_stream_companion/pull/1)。
- 感谢 [AKIhunter](https://github.com/AKIhunter) 在 [PR #63](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot/pull/63) 中分享评论配图和 B站原生表情的思路。
- 感谢 [miss85246](https://github.com/miss85246) 在 [PR #50](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot/pull/50) 中提出评论同步到动态，并核对了接口参数。
- 也感谢 AstrBot 群里各位群友的帮助。

## 📄 License

本项目以 [GNU Affero General Public License v3.0](LICENSE)（AGPL-3.0）发布，与 AstrBot 插件生态的要求一致。

- 修改本插件并通过网络向他人提供服务（例如部署成公开 Bot）时，需要向使用者提供修改后的完整源代码。
- `assets/fonts/` 中的字体按其自带的 [SIL Open Font License](assets/fonts/OFL.txt) 授权。
