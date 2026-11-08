"""
AstrBot Plugin - Bilibili Bot
自动回复评论、好感度、记忆、心情、用户画像、主动视频、动态发布。
这里只是 AstrBot 认的外壳；实现都在 core/ 下。
"""
import sys
import os
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.api.provider import ProviderRequest
from .core import commands
from .core.assembly import assemble
from .core.config import PLUGIN_VERSION
from .core.settings import SettingsAccessor

_ACTIVE_BILIBOT = None


def get_bilibili_ai_bot_api():
    """Return the active v2 memory/profile API for companion plugins."""
    bot = _ACTIVE_BILIBOT
    return getattr(bot, "memory_api", None) if bot is not None else None

_astrbot_site_packages = os.path.join(os.path.expanduser("~"), ".astrbot", "data", "site-packages")
if os.path.isdir(_astrbot_site_packages) and _astrbot_site_packages not in sys.path:
    sys.path.insert(0, _astrbot_site_packages)

@register("astrbot_plugin_bilibili_ai_bot","chenluQwQ,claude","B站 AI Bot — 自动回复评论、好感度、记忆、心情、用户画像、主动视频、动态发布、LLM工具调用",PLUGIN_VERSION,"https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot")
class BiliBiliBot(Star):
    """装配在 ``core/assembly.py``，启停在 ``core/runner.py``，命令在 ``core/commands/``；这里只是 AstrBot 认的外壳。"""

    settings = SettingsAccessor()

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        assemble(self)
        global _ACTIVE_BILIBOT
        _ACTIVE_BILIBOT = self

    async def initialize(self):
        """Start background work only after AstrBot has formally activated the plugin."""
        await self.runner.initialize()

    async def terminate(self):
        await self.runner.terminate()
        global _ACTIVE_BILIBOT
        if _ACTIVE_BILIBOT is self:
            _ACTIVE_BILIBOT = None

    # ===== QQ命令（实现在 core/commands/）=====
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=850)
    async def on_group_bili_share(self, event: AstrMessageEvent):
        async for result in self.app.handle_share(event, trigger_mode="auto"):
            yield result

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=850)
    async def on_private_bili_share(self, event: AstrMessageEvent):
        handled = False
        async for result in self.app.handle_share(event, trigger_mode="auto"):
            handled = True
            yield result
        if handled:
            event.stop_event()

    @filter.command("bili解析")
    async def cmd_bili_parse(self, event: AstrMessageEvent):
        async for result in commands.activities.parse(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili登录")
    async def cmd_login(self, event: AstrMessageEvent):
        async for result in commands.account.login(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili确认")
    async def cmd_login_confirm(self, event: AstrMessageEvent):
        async for result in commands.account.login_confirm(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili状态")
    async def cmd_status(self, event: AstrMessageEvent):
        async for result in commands.overview.status(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili计划")
    async def cmd_schedule(self, event: AstrMessageEvent):
        async for result in commands.overview.schedule(self.app, event):
            yield result

    @filter.command("bili分区")
    async def cmd_regions(self, event: AstrMessageEvent):
        """查看B站分区列表及编号，用于配置视频池"""
        async for result in commands.overview.regions(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili启动")
    async def cmd_start(self, event: AstrMessageEvent):
        async for result in commands.account.start(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili停止")
    async def cmd_stop(self, event: AstrMessageEvent):
        async for result in commands.account.stop(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili主动")
    async def cmd_proactive(self, event: AstrMessageEvent):
        async for result in commands.activities.proactive(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili开关")
    async def cmd_toggle(self, event: AstrMessageEvent):
        async for result in commands.upkeep.toggle(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili刷新")
    async def cmd_refresh_cookie(self, event: AstrMessageEvent):
        async for result in commands.account.refresh_cookie(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili记忆")
    async def cmd_memory(self, event: AstrMessageEvent):
        async for result in commands.memory.memory(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili清算")
    async def cmd_consolidation(self, event: AstrMessageEvent):
        """手动触发日终清算。"""
        async for result in commands.memory.consolidation(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili周总结")
    async def cmd_weekly_summary(self, event: AstrMessageEvent):
        """手动触发周总结（生成并按配置投递）。"""
        async for result in commands.memory.weekly_summary(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili清理老化")
    async def cmd_cleanup_aged(self, event: AstrMessageEvent):
        """删除所有已过保留期的长期记忆"""
        async for result in commands.memory.cleanup_aged(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili联动")
    async def cmd_memory_integration(self, event: AstrMessageEvent):
        """查看给直播伴侣用的记忆接口状态"""
        async for result in commands.memory.integration(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili迁移记忆")
    async def cmd_migrate_memory(self, event: AstrMessageEvent):
        """把没有分级的旧记忆补上分级（聊天记为近期记忆，其他记为长期记忆）"""
        async for result in commands.memory.migrate(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili好感")
    async def cmd_affection(self, event: AstrMessageEvent):
        async for result in commands.people.affection(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili拉黑")
    async def cmd_block(self, event: AstrMessageEvent):
        """手动拉黑用户。用法: /bili拉黑 <UID>"""
        async for result in commands.people.block(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili解黑")
    async def cmd_unblock(self, event: AstrMessageEvent):
        """解除拉黑。用法: /bili解黑 <UID>"""
        async for result in commands.people.unblock(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili黑名单")
    async def cmd_blocklist(self, event: AstrMessageEvent):
        """查看拉黑名单"""
        async for result in commands.people.blocklist(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili清理")
    async def cmd_cleanup(self, event: AstrMessageEvent):
        """清理临时文件和过期数据。用法: /bili清理 [all]"""
        async for result in commands.upkeep.cleanup(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili清理旧")
    async def cmd_stow_old_json(self, event: AstrMessageEvent):
        """把已导入数据库的旧 json 移到 old_json 文件夹（不删除）。先发 /bili清理旧 查看清单，再发 /bili清理旧 确认 执行"""
        async for result in commands.upkeep.stow_old_json(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili导出")
    async def cmd_export(self, event: AstrMessageEvent):
        """把数据库里的数据导出成旧版 json 文件，退回旧版本或备份时用"""
        async for result in commands.upkeep.export(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili性格")
    async def cmd_personality(self, event: AstrMessageEvent):
        """查看性格演化记录。用法: /bili性格"""
        async for result in commands.persona.personality(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili兴趣")
    async def cmd_interest(self, event: AstrMessageEvent):
        """查看近期视频口味、具体兴趣证据和已沉淀偏好。"""
        async for result in commands.persona.interest(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili性格编辑")
    async def cmd_personality_edit(self, event: AstrMessageEvent):
        """手动添加/编辑性格。用法:
        /bili性格编辑 习惯 <内容> — 添加说话习惯
        /bili性格编辑 看法 <内容> — 添加看法
        /bili性格编辑 变化 <内容> — 添加成长变化"""
        async for result in commands.persona.personality_edit(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili性格删除")
    async def cmd_personality_delete(self, event: AstrMessageEvent):
        """删除性格演化条目。用法:
        /bili性格删除 习惯 <序号>
        /bili性格删除 看法 <序号>
        /bili性格删除 变化 <序号>"""
        async for result in commands.persona.personality_delete(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili性格回滚")
    async def cmd_personality_rollback(self, event: AstrMessageEvent):
        """撤销最近一次自动更新的近期状态，不影响核心人设和手动添加的条目"""
        async for result in commands.persona.personality_rollback(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili测试查询")
    async def cmd_test_comment_context(self, event: AstrMessageEvent):
        """试一次评论回复前的查资料流程，不发评论。用法: /bili测试查询 BV号 [问题]"""
        async for result in commands.logs.test_query(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili日志")
    async def cmd_daily_log(self, event: AstrMessageEvent):
        """统一日志入口。用法: /bili日志 <视频|番剧|动态|回复> [日期YYYY-MM-DD]"""
        async for result in commands.logs.daily_log(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili永久记忆")
    async def cmd_permanent_memory(self, event: AstrMessageEvent):
        """查看/删除永久记忆。用法: /bili永久记忆 | /bili永久记忆 删除 <序号>"""
        async for result in commands.memory.permanent(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili备忘录")
    async def cmd_memos(self, event: AstrMessageEvent):
        """查看/删除备忘录。用法: /bili备忘录 | /bili备忘录 删除 <编号>"""
        async for result in commands.memory.memos(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili动态")
    async def cmd_dynamic(self, event: AstrMessageEvent):
        """手动发布动态"""
        async for result in commands.activities.dynamic(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili看番")
    async def cmd_watch_bangumi(self, event: AstrMessageEvent):
        """手动触发看番。用法: /bili看番 [season_id]"""
        async for result in commands.activities.watch_bangumi(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili番剧记忆")
    async def cmd_bangumi_memory(self, event: AstrMessageEvent):
        """查看番剧追番记忆。用法: /bili番剧记忆"""
        async for result in commands.memory.bangumi(self.app, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("biliUMO")
    async def cmd_umo(self, event: AstrMessageEvent):
        """获取当前会话 UMO 并自动记录到周总结/恶意告警配置。"""
        async for result in commands.account.umo(self.app, event):
            yield result

    @filter.command("bili帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        async for result in commands.overview.help_text(self.app, event):
            yield result

    @filter.command("bili绑定")
    async def cmd_bind(self, event: AstrMessageEvent):
        """绑定QQ与B站UID: /bili绑定 12345"""
        async for result in commands.account.bind(self.app, event):
            yield result

    @filter.command("bili解绑")
    async def cmd_unbind(self, event: AstrMessageEvent):
        """解除QQ与B站绑定"""
        async for result in commands.account.unbind(self.app, event):
            yield result

    @filter.on_llm_request()
    async def inject_bili_memory(self, event: AstrMessageEvent, req: ProviderRequest):
        """QQ对话自动注入B站侧记忆：永久记忆 + 语义检索相关记忆"""
        await commands.qq_context.inject_bili_memory(self.app, event, req)
