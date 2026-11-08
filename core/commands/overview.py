"""看一眼：运行状态、今天的计划、视频池分区填法、命令帮助。"""
from __future__ import annotations

from datetime import datetime

from ..config import BILI_ZONES, PLUGIN_VERSION, PRIVATE_MESSAGE_MIN_INTERVAL


async def status(app, event):
    valid,info=await app.check_cookie()
    mood,_=app.today_mood()
    env = app.environment_status()
    cmd_status = env["external_commands"]
    feature_status = env["features"]
    mc = await app.count_memories()
    levels = {key: await app.count_memories(where=lambda m, key=key: m.get("level") == key) for key in ("today", "recent", "long_term")}
    levels["aged"] = await app.count_memories(where=lambda m: bool(m.get("aged")))
    pc=len(app.user_profiles)
    pmc=len(app.permanent_memories())
    evo=app.personality.state()
    evo_ver=evo.get("version",0)
    evo_last=evo.get("last_evolve") or "从未"
    wl=app.watch_log()
    today_watched=len([l for l in wl if l.get("time","").startswith(datetime.now().strftime("%Y-%m-%d"))])
    dl=app.dynamic_log()
    today_dynamic=len([l for l in dl if l.get("time","").startswith(datetime.now().strftime("%Y-%m-%d"))])
    schedule = app.schedule_snapshot()
    live_memory_count = await app.count_memories(types={"live"})
    owner_recommend_delivery = {
        "private_message": "B站私信",
        "comment": "评论区@",
        "both": "私信+评论区",
        "off": "关闭",
    }.get(app.owner_recommend_delivery(), "B站私信")
    private_scope = {
        "owner": "仅主人",
        "whitelist": "主人+白名单",
        "all": "全部安全用户",
    }.get(
        str(app.settings.get("PRIVATE_MESSAGE_REPLY_SCOPE") or "owner").lower(),
        "关闭",
    )
    try:
        private_active_interval = max(
            PRIVATE_MESSAGE_MIN_INTERVAL,
            int(
                app.settings.get("PRIVATE_MESSAGE_POLL_INTERVAL")
                or PRIVATE_MESSAGE_MIN_INTERVAL
            ),
        )
        private_idle_interval = max(
            private_active_interval,
            int(
                app.settings.get("PRIVATE_MESSAGE_IDLE_POLL_INTERVAL")
                or 180
            ),
        )
    except (TypeError, ValueError):
        private_active_interval, private_idle_interval = PRIVATE_MESSAGE_MIN_INTERVAL, 180
    runtime_status = await app.event_runtime.snapshot()
    runtime_priorities = runtime_status.get("event_priorities", {})
    runtime_actions = runtime_status.get("action_states", {})
    lines = [
        f"📺 BiliBot {PLUGIN_VERSION} 状态","━━━━━━━━━━━━",f"🍪 {info}",
        f"{'🟢 运行中' if app.is_running() else '🔴 未运行'}",
        f"🧠 记忆:{mc}条 | 💎永久:{pmc}条 | 👤档案:{pc}个",
        f"   📊 今日:{levels['today']} | 近期:{levels['recent']} | 长期:{levels['long_term']} | 老化:{levels['aged']}",
        f"🎭 心情:{mood} | 🌱性格v{evo_ver}（{evo_last[:10]}）",
        f"📹 今日已看:{today_watched}个视频 | 📝动态:{today_dynamic}条",
        f"🎯 主动时间:{', '.join(schedule['proactive_times']) if schedule['proactive_times'] else '未生成'}",
        f"📢 动态时间:{', '.join(schedule['dynamic_times']) if schedule['dynamic_times'] else '未生成'}",
        f"⭐ 特关时间:{', '.join(schedule['special_follow_times']) if schedule.get('special_follow_times') else '未启用'}",
        f"✅ 已触发主动:{', '.join(schedule['proactive_triggered']) if schedule['proactive_triggered'] else '暂无'}",
        f"✅ 已触发动态:{', '.join(schedule['dynamic_triggered']) if schedule['dynamic_triggered'] else '暂无'}",
        f"回复:{'✅' if app.settings.get('ENABLE_REPLY') else '❌'} 好感:{'✅' if app.settings.get('ENABLE_AFFECTION') else '❌'} 心情:{'✅' if app.settings.get('ENABLE_MOOD') else '❌'}",
        f"✉️ B站私信:{'✅' if app.settings.get('ENABLE_PRIVATE_MESSAGES') else '❌'} 回复:{'✅' if app.settings.get('PRIVATE_MESSAGE_AUTO_REPLY') else '❌'}({private_scope}) 模型按需查询:{'✅' if app.settings.get('PRIVATE_MESSAGE_BILI_SEARCH_ENABLED') else '❌'} 视频先看:{'✅' if app.settings.get('PRIVATE_MESSAGE_AUTO_WATCH_VIDEO') else '❌'} 上下文重置:new 间隔:活跃{private_active_interval}/空闲{private_idle_interval}秒 危险拉黑:{'✅' if app.settings.get('PRIVATE_MESSAGE_AUTO_BLOCK') else '❌'}",
        f"主动:{'✅' if app.settings.get('ENABLE_PROACTIVE') else '❌'} 动态:{'✅' if app.settings.get('ENABLE_DYNAMIC') else '❌'} 特关:{'✅' if app.settings.get('SPECIAL_FOLLOW_ENABLED') else '❌'} 演化:{'✅' if app.settings.get('ENABLE_PERSONALITY_EVOLUTION') else '❌'} 工具:{'✅' if app.settings.get('ENABLE_LLM_TOOLS') else '❌'}",
        f"✉️ 主人推荐:{owner_recommend_delivery} | 最低{app.settings.get('RECOMMEND_OWNER_MIN_SCORE')}分 | 每日上限:{app.settings.get('RECOMMEND_OWNER_DAILY_LIMIT')}",
        f"🔍 联网搜索:{'✅ '+feature_status['web_search_backend'] if feature_status['web_search'] else '❌'} 判断模型:{'✅' if feature_status['web_search_judge'] else '❌(用主模型)'}",
        f"🎨 动态配图:{'✅ '+feature_status['image_gen_backend'] if feature_status['dynamic_image_generation'] else '❌'}{'（仅手动命令）' if feature_status.get('image_gen_backend') == 'novelai' else ''}",
        f"🧭 看片来源:关注 → 搜索(Bot自主决定) → 视频池({app.video_pool_summary()})",
        f"🔗 群/私聊解析:{'✅' if app.settings.get('ENABLE_BILI_SHARE_PARSE') else '❌'} 发原视频:{'✅' if app.settings.get('BILI_SHARE_PARSE_SEND_VIDEO') else '❌'}",
        f"   解析触发 自动:{'✅' if app.settings.get('BILI_SHARE_PARSE_AUTO_TRIGGER_ENABLED') else '❌'} 手动:{'✅' if app.settings.get('BILI_SHARE_PARSE_MANUAL_TRIGGER_ENABLED') else '❌'} LLM:{'✅' if app.settings.get('BILI_SHARE_PARSE_LLM_TRIGGER_ENABLED') else '❌'}",
        f"🎙️ 直播记忆:{live_memory_count}条 | 外部接口:v{app.memory_api.api_version}",
        f"🧱 统一调度:事件处理中{runtime_status['event_states']['processing']} | 队列{runtime_status.get('queue_depth', 0)} | 动作成功{runtime_actions.get('succeeded', 0)} | 失败{runtime_actions.get('failed', 0)} | 结果未知{runtime_actions.get('unknown', 0)}",
        f"🧭 近期优先级:管理员{runtime_priorities.get('admin', 0)} | @提及{runtime_priorities.get('direct_mention', 0)} | 对话{runtime_priorities.get('active_conversation', 0)} | 兴趣{runtime_priorities.get('interesting', 0)} | 普通{runtime_priorities.get('normal', 0)} | 后台{runtime_priorities.get('background', 0)}",
        f"🧭 看片筛选:{'✅' if app.settings.get('ENABLE_PROACTIVE_LLM_PREFILTER') else '❌'} 最多拒绝:{app.settings.get('PROACTIVE_LLM_PREFILTER_MAX_REJECTS')}次 | 分区口味:{app.taste_window_days()}天",
        f"🎞️ 视频分段:{app.settings.get('VIDEO_SEGMENT_MINUTES')}分钟/段，最多{app.settings.get('VIDEO_SEGMENT_MAX_COUNT')}段",
        f"视频视觉Provider:{'✅' if env['llm']['video_provider'] else '❌'} 独立API:{'✅' if env['llm']['video_api'] else '❌'}",
        f"图片识别Provider:{'✅' if env['llm']['image_provider'] else '❌'} 独立API:{'✅' if env['llm']['image_api'] else '❌'}",
        f"外部命令 yt-dlp:{'✅' if cmd_status['yt-dlp'] else '❌'} ffmpeg:{'✅' if cmd_status['ffmpeg'] else '❌'} ffprobe:{'✅' if cmd_status['ffprobe'] else '❌'}",
        f"主动视频直读/截帧:{'✅' if feature_status['proactive_video_media'] else '❌'} 纯文本回退:{'✅' if feature_status['proactive_video_fallback_text'] else '❌'}",
    ]
    yield event.plain_result("\n".join(lines))


async def schedule(app, event):
    schedule = app.schedule_snapshot()
    lines = [
        f"📅 今日计划：{schedule['date']}",
        "━━━━━━━━━━━━",
        f"🎯 主动看视频时间：{', '.join(schedule['proactive_times']) if schedule['proactive_times'] else '未生成'}",
        f"✅ 已触发主动：{', '.join(schedule['proactive_triggered']) if schedule['proactive_triggered'] else '暂无'}",
        f"📢 动态发布时间：{', '.join(schedule['dynamic_times']) if schedule['dynamic_times'] else '未生成'}",
        f"✅ 已触发动态：{', '.join(schedule['dynamic_triggered']) if schedule['dynamic_triggered'] else '暂无'}",
        f"🎬 看番时间：{', '.join(schedule['bangumi_times']) if schedule.get('bangumi_times') else '未生成'}",
        f"✅ 已触发看番：{', '.join(schedule['bangumi_triggered']) if schedule.get('bangumi_triggered') else '暂无'}",
        f"⭐ 特关巡视时间：{', '.join(schedule['special_follow_times']) if schedule.get('special_follow_times') else '未启用'}",
        f"✅ 已触发特关：{', '.join(schedule['special_follow_triggered']) if schedule.get('special_follow_triggered') else '暂无'}",
    ]
    yield event.plain_result("\n".join(lines))


async def regions(app, event):
    lines = ["📂 B站分区列表", "━━━━━━━━━━━━",
              "视频池可直接填中文：热门、推荐、每周必看、入站必刷",
              "分区示例：排行榜:游戏 / 最新:单机游戏 / 游戏 / 单机游戏",
              "仍兼容旧写法：ranking:4 / newlist:17；多个用逗号分隔", ""]
    for rid, zone in BILI_ZONES.items():
        lines.append(f"📁 {zone['name']}（可填：{zone['name']} / 排行榜:{zone['name']}，rid:{rid}）")
        if zone["children"]:
            subs = [f"{name}（最新:{name}，tid:{tid}）" for tid, name in zone["children"].items()]
            lines.append("  └ " + "、".join(subs))
    text = "\n".join(lines)
    if len(text) > 2000:
        mid_idx = len(lines) // 2
        yield event.plain_result("\n".join(lines[:mid_idx]))
        yield event.plain_result("\n".join(lines[mid_idx:]))
    else:
        yield event.plain_result(text)


async def help_text(app, event):
    yield event.plain_result("📺 BiliBot 命令\n━━━━━━━━━━━━\n/bili登录 — 扫码登录\n/bili确认 — 确认扫码\n/bili状态 — 运行状态\n/bili计划 — 查看今日主动/动态/看番时间\n/bili分区 — 查看视频池中文填法和分区名\n/bili启动 — 启动\n/bili停止 — 停止\n/bili主动 — 立刻触发一次主动看视频\n/bili解析 [链接/BV号] — 解析指定、引用或上一个视频\n/bili开关 — 功能开关\n/bili刷新 — 刷新Cookie\n/bili记忆 — 搜索记忆\n/bili好感 — 好感度\n/bili拉黑 — 手动拉黑\n/bili解黑 — 解除拉黑\n/bili黑名单 — 查看黑名单\n/bili兴趣 — 查看近期视频口味与已沉淀偏好\n/bili性格 — 查看性格演化\n/bili性格编辑 — 手动编辑性格\n/bili性格删除 — 删除演化条目\n/bili日志 视频 — 主动看视频&评论记录\n/bili日志 番剧 — 看番记录\n/bili日志 动态 — 动态发布记录\n/bili日志 回复 — 评论回复记录\n/bili日志 查询 — 自主查询与实测记录\n/bili测试查询 BV号 [问题] — 只读测试查询流程\n/bili开关 查询 — 自主查询总开关\n/bili开关 解析 — 视频解析总开关\n/bili开关 自动解析 — 聊天链接自动解析开关\n/bili开关 手动解析 — /bili解析 命令开关\n/bili开关 LLM解析 — bili_parse_video 工具开关\n/bili开关 解析视频 — 是否发送原视频切片\n/bili开关 筛选 — 主动看视频前标题筛选\n/bili联动 — 查看直播伴侣联动状态\n/bili看番 — 手动触发看番\n/bili番剧记忆 — 查看追番进度\n/bili永久记忆 — 查看/删除永久记忆\n/bili备忘录 — 查看/删除备忘录\n/bili动态 — 手动发动态\n/bili绑定 — 绑定QQ与B站UID\n/bili解绑 — 解除绑定\n/bili清理 — 清理临时文件\n/bili导出 — 数据导出成旧版 json\n/bili清理旧 — 把已导入的旧 json 移到 old_json 文件夹\n/bili帮助 — 本帮助\n/biliUMO — 获取当前UMO并自动填入配置\n━━━━━━━━━━━━\n💡 首次用 /bili登录\n💡 视频池配置不会背编号时，用 /bili分区 查中文填法")
