{
  "USE_ASTRBOT_PERSONA": {
    "description": "【人设】使用AstrBot自带人设",
    "type": "bool",
    "default": true
  },
  "CUSTOM_SYSTEM_PROMPT": {
    "description": "【人设】自定义系统提示词（USE_ASTRBOT_PERSONA关闭时使用）",
    "type": "text",
    "default": "你是一个B站UP主的AI助手，负责回复评论。"
  },
  "LLM_PROVIDER_ID": {
    "description": "【人设】选择用于回复/记忆压缩的LLM（留空用AstrBot默认）",
    "type": "string",
    "default": "",
    "_special": "select_provider"
  },
  "SESSDATA": {
    "description": "【账号】B站 Cookie SESSDATA（/bili登录 扫码自动填入）",
    "type": "string",
    "default": ""
  },
  "BILI_JCT": {
    "description": "【账号】B站 Cookie bili_jct（扫码自动填入）",
    "type": "string",
    "default": ""
  },
  "DEDE_USER_ID": {
    "description": "【账号】Bot的B站UID（扫码自动填入）",
    "type": "string",
    "default": ""
  },
  "REFRESH_TOKEN": {
    "description": "【账号】B站 refresh_token（扫码自动填入）",
    "type": "string",
    "default": ""
  },
  "OWNER_MID": {
    "description": "【账号】主人的B站UID（好感度特殊处理）",
    "type": "string",
    "default": ""
  },
  "OWNER_NAME": {
    "description": "【账号】主人名称（用于提示词）",
    "type": "string",
    "default": ""
  },
  "OWNER_BILI_NAME": {
    "description": "【账号】主人的B站昵称（用于@推荐）",
    "type": "string",
    "default": ""
  },
  "ENABLE_REPLY": {
    "description": "【功能开关】启用评论自动回复",
    "type": "bool",
    "default": true
  },
  "ENABLE_AFFECTION": {
    "description": "【功能开关】启用好感度系统",
    "type": "bool",
    "default": true
  },
  "ENABLE_MOOD": {
    "description": "【功能开关】启用心情系统",
    "type": "bool",
    "default": true
  },
  "ENABLE_PROACTIVE": {
    "description": "【功能开关】启用主动看视频/评论",
    "type": "bool",
    "default": false
  },
  "ENABLE_DYNAMIC": {
    "description": "【功能开关】启用自动发动态",
    "type": "bool",
    "default": false
  },
  "ENABLE_PERSONALITY_EVOLUTION": {
    "description": "【功能开关】启用性格演化（每日反思）",
    "type": "bool",
    "default": true
  },
  "POLL_INTERVAL": {
    "description": "【回复】评论轮询间隔（秒）",
    "type": "int",
    "default": 20,
    "hint": "建议 15-60 秒"
  },
  "CUSTOM_REPLY_INSTRUCTION": {
    "description": "【回复】回复评论的额外指令（追加到回复提示词末尾，留空用默认）",
    "type": "text",
    "default": "",
    "hint": "例如：回复要带一点傲娇感，偶尔用颜文字。这段文字会附加到每次回复评论的提示词中。"
  },
  "PROACTIVE_VIDEO_COUNT": {
    "description": "【主动行为】每天主动看几个视频",
    "type": "int",
    "default": 3
  },
  "PROACTIVE_COMMENT_COUNT": {
    "description": "【主动行为】每天主动评论几条",
    "type": "int",
    "default": 2
  },
  "PROACTIVE_TIMES_COUNT": {
    "description": "【主动行为】每天触发几次主动行为",
    "type": "int",
    "default": 2
  },
  "PROACTIVE_FOLLOW_UIDS": {
    "description": "【主动行为】特别关注的UP主UID列表",
    "type": "list",
    "default": [],
    "hint": "这些UP主的新视频会优先观看"
  },
  "PROACTIVE_VIDEO_POOLS": {
    "description": "【主动行为】主动视频来源池",
    "type": "list",
    "default": ["popular"],
    "hint": "可选: popular(综合热门) / weekly(每周必看) / precious(入站必刷) / ranking(全站排行) / ranking:分区rid / newlist:子分区tid。用 /bili分区 查看编号"
  },
  "PROACTIVE_LIKE": {
    "description": "【主动行为】主动点赞（≥6分）",
    "type": "bool",
    "default": true
  },
  "PROACTIVE_COIN": {
    "description": "【主动行为】主动投币（≥8分）",
    "type": "bool",
    "default": false
  },
  "PROACTIVE_FAV": {
    "description": "【主动行为】主动收藏（≥8分）",
    "type": "bool",
    "default": true
  },
  "PROACTIVE_FOLLOW": {
    "description": "【主动行为】主动关注UP主（≥9分）",
    "type": "bool",
    "default": true
  },
  "PROACTIVE_COMMENT": {
    "description": "【主动行为】主动评论（≥7分）",
    "type": "bool",
    "default": true
  },
  "CUSTOM_PROACTIVE_INSTRUCTION": {
    "description": "【主动行为】主动评论的额外指令",
    "type": "text",
    "default": "",
    "hint": "例如：评论风格偏毒舌但不恶意。这段文字会附加到主动评论的提示词中。"
  },
  "DYNAMIC_TIMES_COUNT": {
    "description": "【动态发布】每天触发几次动态发布",
    "type": "int",
    "default": 1,
    "hint": "建议 1-3 次"
  },
  "DYNAMIC_DAILY_COUNT": {
    "description": "【动态发布】每天最多发几条动态",
    "type": "int",
    "default": 1,
    "hint": "防止刷屏"
  },
  "DYNAMIC_TOPICS": {
    "description": "【动态发布】动态主题池（每行一个）",
    "type": "list",
    "default": [],
    "hint": "留空使用默认主题池"
  },
  "CUSTOM_DYNAMIC_INSTRUCTION": {
    "description": "【动态发布】发动态的额外指令",
    "type": "text",
    "default": "",
    "hint": "例如：动态风格偏文艺，多用比喻。这段文字会附加到动态生成的提示词中。"
  },
  "EVOLVE_HOUR": {
    "description": "【性格演化】触发时间（0-23点）",
    "type": "int",
    "default": 1,
    "hint": "建议设在休眠时段，如凌晨1点"
  },
  "EVOLVE_MAX_RETRIES": {
    "description": "【性格演化】失败后的最大重试次数",
    "type": "int",
    "default": 2,
    "hint": "默认失败 2 次后跳过当日演化"
  },
  "EVOLVE_PROMPT": {
    "description": "【性格演化】自定义性格演化提示词（留空用默认）",
    "type": "text",
    "default": "",
    "hint": "可用变量：{old_traits} {old_habits} {old_opinions} {recent_texts} {owner_name}。回复必须为JSON格式含 new_trait/trigger/speech_habits/opinions/reflection 字段"
  },
  "EMBED_API_KEY": {
    "description": "【记忆】Embedding API Key（记忆向量化用）",
    "type": "string",
    "default": ""
  },
  "EMBED_API_BASE": {
    "description": "【记忆】Embedding API Base URL",
    "type": "string",
    "default": "https://api.siliconflow.cn/v1"
  },
  "EMBED_MODEL": {
    "description": "【记忆】Embedding 模型名称",
    "type": "string",
    "default": "BAAI/bge-m3"
  },
  "VIDEO_VISION_PROVIDER_ID": {
    "description": "【视觉模型】视频分析使用的 AstrBot 模型提供商（优先使用，失败退回独立 API）",
    "type": "string",
    "default": "",
    "_special": "select_provider"
  },
  "VIDEO_VISION_API_KEY": {
    "description": "【视觉模型】视频分析 API Key",
    "type": "string",
    "default": "",
    "hint": "留空则回退为纯文本LLM分析"
  },
  "VIDEO_VISION_API_BASE": {
    "description": "【视觉模型】视频分析 API Base URL",
    "type": "string",
    "default": ""
  },
  "VIDEO_VISION_MODEL": {
    "description": "【视觉模型】视频分析模型名称",
    "type": "string",
    "default": "",
    "hint": "如 google/gemma-3-27b-it 等；主动看视频的直读/截帧分析还需要本机安装 yt-dlp 和 ffmpeg"
  },
  "IMAGE_VISION_PROVIDER_ID": {
    "description": "【视觉模型】图片识别使用的 AstrBot 模型提供商",
    "type": "string",
    "default": "",
    "_special": "select_provider"
  },
  "IMAGE_VISION_API_KEY": {
    "description": "【视觉模型】图片识别 API Key",
    "type": "string",
    "default": "",
    "hint": "留空则不识别评论图片"
  },
  "IMAGE_VISION_API_BASE": {
    "description": "【视觉模型】图片识别 API Base URL",
    "type": "string",
    "default": ""
  },
  "IMAGE_VISION_MODEL": {
    "description": "【视觉模型】图片识别模型名称",
    "type": "string",
    "default": "",
    "hint": "如 google/gemma-3-27b-it 等"
  },
  "ENABLE_WEB_SEARCH": {
    "description": "【联网搜索】启用联网搜索",
    "type": "bool",
    "default": false
  },
  "WEB_SEARCH_JUDGE_PROVIDER_ID": {
    "description": "【联网搜索】搜索判断模型（建议选轻量模型）",
    "type": "string",
    "default": "",
    "_special": "select_provider"
  },
  "WEB_SEARCH_BACKEND": {
    "description": "【联网搜索】搜索后端",
    "type": "string",
    "default": "tavily",
    "hint": "可选：tavily / perplexity / bocha / custom"
  },
  "WEB_SEARCH_API_KEY": {
    "description": "【联网搜索】搜索 API Key",
    "type": "string",
    "default": ""
  },
  "WEB_SEARCH_API_BASE": {
    "description": "【联网搜索】自定义搜索接口地址（仅 custom 后端）",
    "type": "string",
    "default": ""
  },
  "WEB_SEARCH_MODEL": {
    "description": "【联网搜索】搜索模型名（仅 custom / perplexity 后端）",
    "type": "string",
    "default": "",
    "hint": "Perplexity 默认 sonar；custom 按你的接口填写"
  },
  "WEB_SEARCH_MAX_RESULTS": {
    "description": "【联网搜索】搜索返回最大条数",
    "type": "int",
    "default": 5,
    "hint": "建议 3-8"
  },
  "IMAGE_GEN_API_KEY": {
    "description": "【图片生成】API Key（动态配图用）",
    "type": "string",
    "default": "",
    "hint": "留空则复用 VIDEO_VISION_API_KEY"
  },
  "IMAGE_GEN_API_BASE": {
    "description": "【图片生成】API Base URL",
    "type": "string",
    "default": ""
  },
  "IMAGE_GEN_MODEL": {
    "description": "【图片生成】模型名称",
    "type": "string",
    "default": "",
    "hint": "如 Flux 系列模型"
  },
  "SLEEP_START": {
    "description": "【系统】休眠开始时间（0-23）",
    "type": "int",
    "default": 2
  },
  "SLEEP_END": {
    "description": "【系统】休眠结束时间（0-23）",
    "type": "int",
    "default": 8
  },
  "COOKIE_AUTO_REFRESH": {
    "description": "【系统】Cookie过期自动刷新",
    "type": "bool",
    "default": true
  },
  "COOKIE_CHECK_INTERVAL": {
    "description": "【系统】Cookie检查间隔（小时）",
    "type": "int",
    "default": 6
  },
  "ENABLE_WEB_PANEL": {
    "description": "【系统】启用Web管理面板",
    "type": "bool",
    "default": false
  },
  "WEB_PANEL_PORT": {
    "description": "【系统】Web面板端口",
    "type": "int",
    "default": 5001
  },
  "WEB_PANEL_PASSWORD": {
    "description": "【系统】Web面板登录密码",
    "type": "string",
    "default": "admin123"
  }
}
