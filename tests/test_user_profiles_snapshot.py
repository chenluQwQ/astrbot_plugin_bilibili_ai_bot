"""用户画像：从旧数据样例出发，跑一遍各处会用到的读写，逐条比对快照。

读：整理后的画像、各个场景能看到的「对该用户的了解」。
写：印象 / 事实 / 标签、评论区相遇、视频引用（含重复）、直播互动、主动看片关联 UP 主、
关系计数、删记忆后清直播引用。最后比对整份文件。

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_user_profiles_snapshot.py
"""
from base_parts import BaseParts
import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.storage.json_file import JsonFile

import core.domain.user_profiles as profiles_module

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "legacy_data" / "user_profiles.json"
SNAPSHOT_FILE = ROOT / "tests" / "snapshots" / "user_profiles.json"
SCOPES = ["bili_comment", "bili_dm", "bili_live", "qq_group", "qq_private", "proactive", "admin"]


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 3, 9, 30)


class Bot(BaseParts):
    def _load_json(self, path, default=None):
        return JsonFile(path).read(default)

    def _save_json(self, path, data):
        JsonFile(path).write(data)

    def _remove_profile_memory_refs(self, removed_rpids):
        # 原来 MemoryMixin 上的转发；删记忆、清算现在直接调 user_profiles.remove_memory_refs
        return self.user_profiles.remove_memory_refs(removed_rpids)


class UserProfilesSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "user_profiles.json"
        shutil.copy(FIXTURE, self.path)
        self.stack = ExitStack()
        for module in (profiles_module,):
            if hasattr(module, "USER_PROFILE_FILE"):
                self.stack.enter_context(patch.object(module, "USER_PROFILE_FILE", str(self.path)))
            if hasattr(module, "datetime"):
                self.stack.enter_context(patch.object(module, "datetime", _FrozenDatetime))
        self.bot = Bot()
        self.bot.user_profiles = profiles_module.UserProfiles(JsonFile(self.path))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def _reads(self, uids):
        return {
            uid: {
                "normalized": profiles_module.normalize(
                    json.loads(self.path.read_text(encoding="utf-8")).get(uid)
                ),
                "context": {scope: self.bot.user_profiles.context(uid, scope) for scope in SCOPES},
            }
            for uid in uids
        }

    def test_matches_snapshot(self):
        bot = self.bot
        uids = sorted(json.loads(FIXTURE.read_text(encoding="utf-8")))
        results = {"before": self._reads(uids + ["0", "no-such-user"])}

        first, second = uids[0], uids[1]
        bot.user_profiles.update(
            first, username="新名字", impression="私信里很客气",
            new_facts=["  喜欢猫 ", "喜欢猫", ""], new_tags=["猫派", " "], source_scope="bili_dm",
        )
        bot.user_profiles.update(first, new_facts=[f"事实{i}" for i in range(25)], new_tags=[f"标签{i}" for i in range(12)])
        bot.user_profiles.update(second, video_encounter={"bvid": "BV1aaaaaaaaa", "title": "评论区"})
        bot.user_profiles.update(second, video_ref={"bvid": "BV1aaaaaaaaa", "relation": "commented_under", "title": "又一次"})
        bot.user_profiles.update(second, video_ref={"bvid": "BV1bbbbbbbbb"}, source_scope="qq_group")
        for i in range(3):
            bot.user_profiles.update(
                "777", username="直播观众",
                live_event={"event_type": "danmaku" if i < 2 else "gift", "session_id": f"s{i}", "memory_ref": f"ref-{i % 2}"},
                source_scope="bili_live",
            )
        bot.user_profiles.link_video("888", "UP主", "BV1ccccccccc", title="主动看到的")
        bot.user_profiles.link_video("0", "无效", "BV1ddddddddd")
        bot.user_profiles.link_video("889", "无视频", "")
        relations = [
            bot.user_profiles.record_interaction(first, "", delta, source)
            for delta, source in ((2, "comment"), (-3, "private"), (-4, "private"), (-1, "comment"), (0, "comment"))
        ]
        removed = bot._remove_profile_memory_refs(["ref-0", "", None])
        removed_none = bot._remove_profile_memory_refs([])

        results["relations"] = relations
        results["removed"] = [removed, removed_none]
        results["after"] = self._reads([first, second, "777", "888"])
        results["file"] = json.loads(self.path.read_text(encoding="utf-8"))

        if os.environ.get("BILIBOT_UPDATE_SNAPSHOTS") or not SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        self.maxDiff = None
        self.assertEqual(results, json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
