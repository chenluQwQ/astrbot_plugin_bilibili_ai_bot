"""Offline regression tests for the 2026-10-06 audit fixes owned by main agent.

Covers:
- F2: management commands must carry the ADMIN permission filter.
- F3: /bili绑定 cannot self-declare OWNER_MID; owner context injection needs admin.
- #34: schedule plan model is configurable, retries are configurable, and the
  plan JSON extractor falls back to the LLM JSON repair helper.

All tests are offline: no AstrBot instance, no network, fabricated config only.
"""
import ast
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from base_parts import attach_base
from schedule_parts import ScheduleParts
from core.runtime import WriteResultUnknownError
from core.settings import Settings
from core.domain.affection import Affection
from core.domain.blocklist import BlockList
from core.infra.bili.users import BiliUsers
from core.infra.text import extract_json_object
from core.storage.json_file import JsonFile
from command_modules import app_for, load as load_command_module

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"

ADMIN_COMMANDS = {
    "cmd_login", "cmd_login_confirm", "cmd_start", "cmd_stop",
    "cmd_proactive", "cmd_toggle", "cmd_refresh_cookie", "cmd_consolidation",
    "cmd_weekly_summary", "cmd_cleanup_aged", "cmd_block", "cmd_unblock",
    "cmd_cleanup", "cmd_personality_edit", "cmd_personality_delete",
    "cmd_permanent_memory", "cmd_dynamic", "cmd_watch_bangumi", "cmd_umo",
    "cmd_status", "cmd_schedule", "cmd_memory", "cmd_memory_integration",
    "cmd_migrate_memory", "cmd_affection", "cmd_interest", "cmd_blocklist",
    "cmd_personality", "cmd_personality_rollback", "cmd_daily_log",
    "cmd_bangumi_memory",
}

USER_FACING_COMMANDS = {
    "cmd_bind", "cmd_unbind", "cmd_help", "cmd_bili_parse", "cmd_regions",
}


def _main_class_tree():
    tree = ast.parse(MAIN.read_text(encoding="utf-8-sig"))
    return next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BiliBiliBot"
    )


class AdminPermissionDecoratorTests(unittest.TestCase):
    def test_management_commands_require_admin(self):
        cls = _main_class_tree()
        decorated = {}
        for node in cls.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [ast.unparse(d) for d in node.decorator_list]
            if any("command(" in d for d in decorators):
                decorated[node.name] = decorators
        for name in sorted(ADMIN_COMMANDS):
            with self.subTest(command=name):
                self.assertIn(name, decorated, f"{name} 不再是命令？测试需要更新")
                self.assertTrue(
                    any("permission_type" in d and "ADMIN" in d for d in decorated[name]),
                    f"{name} 缺少 @filter.permission_type(filter.PermissionType.ADMIN)",
                )

    def test_user_facing_commands_stay_open(self):
        cls = _main_class_tree()
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in USER_FACING_COMMANDS:
                decorators = [ast.unparse(d) for d in node.decorator_list]
                self.assertFalse(
                    any("permission_type" in d for d in decorators),
                    f"{node.name} 是普通用户功能，不应加管理员限制",
                )


class Event:
    def __init__(self, text, sender="20002", admin=False):
        self.message_str = text
        self.sender = sender
        self._admin = admin
        self.unified_msg_origin = f"audit:FriendMessage:{sender}"
        self.message_obj = types.SimpleNamespace(raw_message=None, message=[])

    def plain_result(self, text):
        return text

    def get_sender_id(self):
        return self.sender

    def is_admin(self):
        return self._admin


class BindSecurityTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self, bindings_file, owner_mid="10001"):
        bot = types.SimpleNamespace(
            config={"OWNER_MID": owner_mid},
            _load_json=lambda path, default=None: default if default is not None else {},
            _save_json=MagicMock(),
        )
        attach_base(bot)
        bot.settings = Settings(bot.config)
        account = load_command_module("account")
        return lambda bot, event: account.bind(app_for(bot), event), bot

    async def test_non_admin_cannot_bind_owner_uid(self):
        with tempfile.TemporaryDirectory() as temp:
            cmd_bind, bot = self._bot(Path(temp) / "bindings.json")
            event = Event("/bili绑定 10001", admin=False)
            replies = [item async for item in cmd_bind(bot, event)]
            self.assertIn("管理员", replies[-1])
            bot.json_store._save_json.assert_not_called()

    async def test_admin_can_bind_owner_uid(self):
        with tempfile.TemporaryDirectory() as temp:
            cmd_bind, bot = self._bot(Path(temp) / "bindings.json")
            event = Event("/bili绑定 10001", admin=True)
            replies = [item async for item in cmd_bind(bot, event)]
            self.assertIn("已绑定", replies[-1])
            bot.json_store._save_json.assert_called_once()

    async def test_normal_user_can_bind_own_uid(self):
        with tempfile.TemporaryDirectory() as temp:
            cmd_bind, bot = self._bot(Path(temp) / "bindings.json")
            event = Event("/bili绑定 33333", admin=False)
            replies = [item async for item in cmd_bind(bot, event)]
            self.assertIn("已绑定", replies[-1])


class OwnerInjectionGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_bound_to_owner_gets_no_owner_context(self):
        with tempfile.TemporaryDirectory() as temp:
            bindings = Path(temp) / "bindings.json"
            bindings.write_text(json.dumps({"20002": "10001"}), encoding="utf-8")
            qq_context = load_command_module("qq_context")

            bot = types.SimpleNamespace(
                config={
                    "OWNER_MID": "10001",
                    "ENABLE_CROSS_PLATFORM_ACTIVITY_STATUS": True,
                    "MEMORY_ISOLATION_MODE": "safe_share",
                    "ENABLE_SAFE_CROSS_PLATFORM_MEMORY": True,
                },
                _maybe_trigger_proactive_from_llm=AsyncMock(),
                share_session=types.SimpleNamespace(_inject_recent_group_share_into_request=MagicMock(return_value=False)),
                _load_json=lambda path, default=None: json.loads(bindings.read_text(encoding="utf-8")),
                now_doing=types.SimpleNamespace(prompt=lambda: "OWNER_SECRET_ACTIVITY"),
                qq_chat=types.SimpleNamespace(_inject_context_block_before_user=MagicMock(return_value=True)),
                memory_api=types.SimpleNamespace(
                    get_recent_memories=MagicMock(return_value=[
                        {"memory_type": "chat", "text": "OWNER_SECRET_MEMORY_TEXT"}
                    ])
                ),
            )
            bot.settings = Settings(bot.config)
            req = types.SimpleNamespace(prompt="今天做了什么", system_prompt="")
            event = Event("今天做了什么", admin=False)
            await qq_context.inject_bili_memory(app_for(bot), event, req)
            self.assertNotIn("OWNER_SECRET", req.prompt)
            bot.memory_api.get_recent_memories.assert_not_called()


class _PlanProbe(ScheduleParts):
    """原来这里把 schedule_mixin.py 单独加载，只为了把日程文件指到临时目录；每日计划现在是插件上的对象，
    用不到文件的几项直接用真的，要写文件的那项把 json_store 换成假的。"""


class SchedulePlanConfigTests(unittest.TestCase):
    def test_max_attempts_respects_config(self):
        mixin = _PlanProbe()
        mixin.config = {}
        self.assertEqual(mixin.daily_plan._autonomous_plan_max_attempts(), 3)  # 1 + 2 retries
        mixin.config = {"AUTONOMOUS_PLAN_MAX_RETRIES": 0}
        self.assertEqual(mixin.daily_plan._autonomous_plan_max_attempts(), 1)
        mixin.config = {"AUTONOMOUS_PLAN_MAX_RETRIES": 5}
        self.assertEqual(mixin.daily_plan._autonomous_plan_max_attempts(), 6)
        mixin.config = {"AUTONOMOUS_PLAN_MAX_RETRIES": "junk"}
        self.assertEqual(mixin.daily_plan._autonomous_plan_max_attempts(), 3)

    def test_extract_plan_json_uses_repair_fallback(self):
        # Fenced JSON with trailing commas goes through core.infra.text.repair_llm_json.
        raw = '```json\n{"proactive_windows": ["10:00-11:00",], "rationale": "ok",}\n```'
        plan = extract_json_object(raw)
        self.assertEqual(plan.get("rationale"), "ok")
        self.assertEqual(plan.get("proactive_windows"), ["10:00-11:00"])

    def test_extract_plan_json_finds_object_inside_noise(self):
        plan = extract_json_object('noise {"dynamic_times": ["20:00"]} tail')
        self.assertEqual(plan.get("dynamic_times"), ["20:00"])


class ScheduleProviderPassthroughTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_llm_call_uses_configured_provider(self):
        with tempfile.TemporaryDirectory():
            mixin = _PlanProbe()
            attach_base(mixin)
            captured = {}

            async def fake_llm(prompt, system_prompt="", max_tokens=300, provider_id=None):
                captured["provider_id"] = provider_id
                return '{"proactive_windows": [], "rationale": "quiet"}'

            mixin.config = {
                "ENABLE_AUTONOMOUS_DAILY_PLAN": True,
                "SCHEDULE_PLAN_PROVIDER_ID": "plan-model-01",
                "AUTONOMOUS_PLAN_GENERATION_MODE": "fixed",
                "AUTONOMOUS_PLAN_GENERATION_TIME": "00:00",
                "AUTONOMOUS_MIN_ACTION_GAP_MINUTES": 45,
                "AUTONOMOUS_ACTIVITY_LEVEL": 55,
                "AUTONOMOUS_PROACTIVE_WINDOW_MINUTES": 90,
                "SLEEP_START": 2, "SLEEP_END": 8,
                "PROACTIVE_TIMES_COUNT": 2, "PROACTIVE_DAILY_LIMIT": 4,
                "DYNAMIC_TIMES_COUNT": 1, "DYNAMIC_DAILY_COUNT": 1,
                "BANGUMI_DAILY_LIMIT": 1, "SPECIAL_FOLLOW_TIMES_COUNT": 1,
                "DYNAMIC_WATCH_TIMES_COUNT": 2,
                "AUTONOMOUS_PLAN_PROMPT": "",
            }
            mixin.models = mixin  # 假插件自己写了 _llm_call，经 models 找回它
            mixin._llm_call = fake_llm
            mixin.mood = types.SimpleNamespace(today=lambda: ("平静", ""))
            mixin._get_system_prompt = AsyncMock(return_value="persona")
            mixin.daily_plan._autonomous_generation_due = lambda: True
            mixin.schedule_rules._schedule_feature_enabled = lambda kind: True
            mixin.json_store._load_json = lambda path, default=None: default if default is not None else {}
            saved = {}
            mixin.json_store._save_json = lambda path, data: saved.setdefault(str(path), data)
            mixin.schedule_files._save_schedule_state = lambda *a, **k: None
            mixin.schedule_files._save_dynamic_schedule_state = lambda *a, **k: None
            mixin.schedule_files._save_bangumi_schedule_state = lambda *a, **k: None
            mixin.schedule_files._save_special_follow_schedule_state = lambda *a, **k: None
            mixin.schedule_files._save_dynamic_watch_schedule_state = lambda *a, **k: None

            plan = await mixin.daily_plan._ensure_autonomous_daily_plan(force=True)
            self.assertEqual(captured.get("provider_id"), "plan-model-01")
            self.assertEqual(plan.get("generation_status"), "success")


class UnblockResultSafetyTests(unittest.IsolatedAsyncioTestCase):
    def _command_and_bot(self, directory):
        directory = Path(directory)
        block_file = directory / "block_log.json"
        affection_file = directory / "affection.json"
        block_file.write_text(json.dumps({"123": {"reason": "test"}}), encoding="utf-8")
        affection_file.write_text(json.dumps({"123": 42}), encoding="utf-8")

        def load(path, default=None):
            try:
                return json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {} if default is None else default

        def save(path, value):
            Path(path).write_text(json.dumps(value), encoding="utf-8")

        people = load_command_module("people")
        bot = types.SimpleNamespace(
            config={"BILI_JCT": "csrf"},
            _http_post=AsyncMock(),
            _load_json=load,
            _save_json=save,
        )
        attach_base(bot)
        bot.settings = Settings(bot.config)
        bot.affection = Affection(JsonFile(affection_file), lambda _uid: False)
        bot.blocklist = BlockList(JsonFile(block_file), JsonFile(directory / "block_count.json"))
        # 解黑走真正的B站接口那层，只把最底下的请求换成假的
        bot.bili_users = BiliUsers(bot)
        command = lambda bot, event: people.unblock(app_for(bot), event)
        return command, bot, block_file, affection_file

    async def test_unknown_unblock_preserves_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            command, bot, block_file, affection_file = self._command_and_bot(directory)
            bot.bili_http._http_post.side_effect = WriteResultUnknownError("response lost")
            replies = [item async for item in command(bot, Event("/bili解黑 123"))]
            self.assertIn("结果未知", replies[-1])
            self.assertNotIn("✅ 已解除拉黑", replies[-1])
            self.assertIn("123", json.loads(block_file.read_text(encoding="utf-8")))
            self.assertEqual(42, bot.affection.score("123"))
            self.assertEqual(42, json.loads(affection_file.read_text(encoding="utf-8"))["123"])
            bot.bili_http._http_post.assert_awaited_once()

    async def test_successful_unblock_still_commits_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            command, bot, block_file, affection_file = self._command_and_bot(directory)
            bot.bili_http._http_post.return_value = ({"code": 0}, None)
            replies = [item async for item in command(bot, Event("/bili解黑 123"))]
            self.assertIn("✅ 已解除拉黑", replies[-1])
            self.assertNotIn("123", json.loads(block_file.read_text(encoding="utf-8")))
            self.assertEqual(0, bot.affection.score("123"))
            self.assertEqual(0, json.loads(affection_file.read_text(encoding="utf-8"))["123"])


if __name__ == "__main__":
    unittest.main()
