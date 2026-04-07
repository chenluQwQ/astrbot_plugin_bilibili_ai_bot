# astrbot_plugin_bilibili_bot

B站 AI Bot 插件 for [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 让你的 AI 角色在 B 站评论区”活”起来。

## ✨ 功能

- **评论自动回复** — 轮询评论通知，自动生成 AI 回复
- **好感度系统** — 陌生人 → 粉丝 → 熟人 → 好友 → 主人，不同等级不同语气；辱骂自动拉黑
- **用户画像** — 自动提取用户印象、个人信息、标签
- **语义记忆** — Embedding 向量化 + 余弦相似度检索，支持记忆压缩、永久记忆
- **心情系统** — 每日随机心情 + 节日彩蛋（含农历）
- **视频上下文** — 自动获取评论所在视频信息注入 prompt
- **LLM 熔断保护** — 单条重试 3 次放弃，全局连续 5 次失败冷却 5 分钟
- **Cookie 自动刷新** — 定期检查 + 自动刷新，支持扫码登录
- **拉黑管理** — 手动/自动拉黑，黑名单用户不调 LLM 不花钱

### 🚧 计划中

- 主动看视频 / 点赞 / 投币 / 收藏 / 关注
- 主动评论
- 自动发动态
- 性格演化
- 记忆互通（QQ ↔ B站）

## 📦 安装

在 AstrBot WebUI 的插件市场搜索 `bilibili_bot`，点击安装即可。

或手动安装：

```bash
cd AstrBot/data/plugins
git clone https://github.com/chenluQwQ/astrbot_plugin_bilibili_bot
```

## ⚙️ 配置

安装后在 WebUI 插件配置页面填写：

|配置项              |必填|说明                             |
|-----------------|--|-------------------------------|
|`LLM_PROVIDER_ID`|✅ |选择用于回复的 LLM 模型                 |
|`SESSDATA`       |自动|B站 Cookie（`/bili登录` 扫码自动填入）    |
|`BILI_JCT`       |自动|B站 CSRF Token（扫码自动填入）          |
|`DEDE_USER_ID`   |自动|Bot 的 B站 UID（扫码自动填入）           |
|`REFRESH_TOKEN`  |自动|Cookie 自动刷新用（扫码自动填入）           |
|`OWNER_MID`      |推荐|主人的 B站 UID（好感度特殊处理）            |
|`OWNER_NAME`     |推荐|主人名称（用于 prompt）                |
|`EMBED_API_KEY`  |可选|Embedding API 密钥（记忆向量化用）       |
|`EMBED_API_BASE` |可选|Embedding API 地址，默认 SiliconFlow|
|`EMBED_MODEL`    |可选|Embedding 模型名，默认 `BAAI/bge-m3` |


> 💡 Cookie 获取方式：发送 `/bili登录` 扫码即可，登录后 Cookie 会自动定期刷新，无需手动维护。

## 🎮 命令

|命令             |说明                     |
|---------------|-----------------------|
|`/bili登录`      |扫码登录 B站（扫码后发 `/bili确认`）|
|`/bili确认`      |确认扫码结果                 |
|`/bili状态`      |查看运行状态                 |
|`/bili启动`      |启动 Bot                 |
|`/bili停止`      |停止 Bot                 |
|`/bili开关 <功能>` |切换功能开关                 |
|`/bili刷新`      |手动刷新 Cookie            |
|`/bili记忆 <关键词>`|语义搜索记忆                 |
|`/bili好感 [UID]`|查看好感度排行/查询             |
|`/bili拉黑 <UID>`|手动拉黑用户                 |
|`/bili解黑 <UID>`|解除拉黑                   |
|`/bili黑名单`     |查看黑名单                  |
|`/bili帮助`      |查看帮助                   |

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

## 📁 数据存储

插件数据存储在 `data/plugin_data/astrbot_plugin_bilibili_bot/` 目录下，更新插件不会丢失数据。

## 🔗 相关

- [问题反馈](https://github.com/chenluQwQ/astrbot_plugin_bilibili_bot/issues)

## 🌟支持
如果喜欢，请点个⭐️

## 📄 License

MIT
