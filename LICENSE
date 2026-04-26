# 更新日志

## v1.1.2 (2026-04-26)

### 🔧 Bug 修复

- **动态获取四层 bug 修复**
  - 修复 `dict.get(key, {})` 在值为 `None` 时不返回默认值的问题（经典 Python 陷阱）
  - 修复 `comment_type=11` 时错误地将 doc_id 当作 dynamic_id 传给详情 API
  - 修复 B站动态详情 API 缺少 `features` 参数导致返回空数据
  - 修复 B站 opus 格式动态的文字和图片解析路径（`major.opus.summary.text` / `major.opus.pics`）
- **所有 B站 web-dynamic 系列 API 统一补充 `features=itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote` 参数**
  - `x/polymer/web-dynamic/v1/detail`
  - `x/polymer/web-dynamic/v1/feed/space`
  - `x/polymer/web-dynamic/v1/feed/all`

### ✨ 新增功能

- **工具调用体系重构：FunctionTool 模式**
  - 所有 LLM 工具从 `@filter.llm_tool` 迁移到 `FunctionTool` 类定义（`core/tools.py`）
  - 工具返回结果回到 LLM 上下文，由 LLM 按人设风格重新生成回复，不再直接暴露原始数据给用户

- **新增 15 个 LLM 工具**

  **记忆类（4 个）：**
  - `recall_user` — 查询用户画像 / 印象 / 好感度，支持 UID 或用户名模糊搜索
  - `recall_conversation` — 语义搜索对话记忆，可限定用户
  - `recall_video` — 搜索看过的视频记忆
  - `recall_dynamic` — 搜索动态相关记忆

  **B站查询类（3 个）：**
  - `search_bilibili` — 搜索视频或 UP 主，支持用户想看某类内容时推荐视频
  - `get_up_info` — 查询 UP 主详细信息 + 最近投稿 + 最近动态，支持 UID 或名字输入
  - `watch_video` — 去看一个视频（拉信息 + AI 分析 + 评分 + 存记忆），看完后可链式调用互动工具

  **B站操作类（5 个）：**
  - `post_comment` — 在视频下发评论
  - `like_video` — 点赞
  - `coin_video` — 投币（1 或 2 个）
  - `fav_video` — 收藏到默认收藏夹
  - `follow_up` — 关注 UP 主，支持 UID 或名字输入

  **关注动态类（2 个）：**
  - `check_following_updates` — 查看今天关注的 UP 主有没有人更新（视频 / 动态 / 直播）
  - `check_following_live` — 查看关注的人谁在直播（标题 / 分区 / 人气 / 链接）

  **主动行为（1 个）：**
  - `bili_watch_videos` — 触发一次主动看视频流程

- **新增 B站 API 方法**（`core/bilibili.py`）
  - `search_bilibili_videos` — 视频搜索（WBI 签名）
  - `search_bilibili_users` — 用户搜索（WBI 签名）
  - `get_up_info` — UP 主详细信息（WBI 签名）
  - `get_up_recent_videos` — UP 主最近投稿列表（WBI 签名）
  - `get_up_recent_dynamics` — UP 主最近动态（opus 格式兼容）
  - `get_following_updates` — 关注动态流（今日更新过滤）
  - `get_following_live` — 关注直播列表

### 🎨 优化

- **配置界面重排**（`_conf_schema.json`）
  - 按功能分组并以【标签】前缀标注：人设 → 账号 → 功能开关 → 回复 → 主动行为 → 动态发布 → 性格演化 → 记忆 → 视觉模型 → 联网搜索 → 图片生成 → 系统
  - 核心设置排最前，小功能靠后

- **工具描述优化**
  - 所有工具加入"不确定时先问用户"的行为约束
  - 记忆类工具加入"每次对话只调用一次，查不到就说没有"防止循环调用
  - 操作类工具加入"需要用户同意或主动要求时才使用"的约束
  - Bot 自身 UID 和主人 UID 注入到相关工具描述中

- **视频内容详情长度调整**
  - 工具返回的视频内容详情从 500 字扩展到 800 字
  - 记忆存储的视频内容从 200 字扩展到 500 字
