"""B4b 画像：好感「到过门槛」的标记、回复时攒候选、每晚按人和地方整理画像、严格 JSON。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from test_autonomous_ranges import _install_astrbot_stub  # noqa: F401  先装上假的 astrbot
from core.content_protocol import ContentProtocolError, parse_profile_digest
from core.domain.affection import Affection
from core.domain.profile_candidates import ProfileCandidates
from core.domain.user_profiles import UserProfiles
from core.features import profile_digest
from core.storage.json_file import JsonFile

NOW = 1_800_000_000.0
HOUR = 3600
DAY = 86400


def _now(days=0, hours=0):
    return NOW + days * DAY + hours * HOUR


class _Temp(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)

    def affection(self, line=20):
        return Affection(JsonFile(self.data / "affection.json"), lambda uid: uid == "owner",
                         JsonFile(self.data / "reached.json"), lambda: line)


class ProfileLineTests(_Temp):
    def test_marked_when_first_crossing_and_kept_after_dropping(self):
        affection = self.affection()
        affection.set("1", 19)
        self.assertFalse(affection.reached_profile_line("1"))
        affection.set("1", 20)
        self.assertTrue(affection.reached_profile_line("1"))
        affection.set("1", -5)
        self.assertTrue(affection.reached_profile_line("1"))

    def test_startup_backfills_from_existing_scores(self):
        JsonFile(self.data / "affection.json").write({"1": 35, "2": 10, "3": 20})
        affection = self.affection()
        self.assertEqual(affection.mark_reached_from_scores(), 2)
        self.assertEqual(affection.mark_reached_from_scores(), 0)
        self.assertEqual(sorted(JsonFile(self.data / "reached.json").read({})), ["1", "3"])

    def test_line_is_configurable(self):
        affection = self.affection(line=50)
        affection.set("1", 40)
        self.assertFalse(affection.reached_profile_line("1"))
        affection.set("1", 50)
        self.assertTrue(affection.reached_profile_line("1"))


class CandidateStoreTests(_Temp):
    def setUp(self):
        super().setUp()
        self.reached = {"1", "owner"}
        self.store = ProfileCandidates(JsonFile(self.data / "cand.json"), lambda uid: uid in self.reached,
                                       lambda uid: uid == "owner")

    def test_only_people_past_the_line(self):
        self.assertEqual(self.store.note("2", "bili_comment", "印象", ["事实"], now=NOW), 0)
        self.assertEqual(self.store.note("1", "bili_comment", "印象", ["事实"], now=NOW), 2)
        self.store.note("1", "bili_comment", "", ["又一条"], "小一", now=NOW)
        self.assertEqual(self.store.pending(NOW), [{"uid": "1", "scope": "bili_comment", "username": "小一",
                                                   "impressions": ["印象"], "facts": ["事实", "又一条"], "ts": NOW}])

    def test_five_a_day_except_the_owner(self):
        facts = [f"事实{i}" for i in range(5)]
        self.assertEqual(self.store.note("1", "bili_comment", "印象", facts, now=NOW), 5)
        self.assertEqual(self.store.note("1", "bili_dm", "", ["再一条"], now=NOW), 0)
        self.assertEqual(self.store.note("1", "bili_dm", "", ["第二天"], now=_now(days=1)), 1)
        self.assertEqual(self.store.note("owner", "bili_dm", "印象", facts + ["第六条"], now=NOW), 7)

    def test_same_note_is_kept_once_and_places_stay_apart(self):
        self.store.note("1", "bili_comment", "", ["在考研"], now=NOW)
        self.store.note("1", "bili_comment", "", ["在考研"], now=NOW)
        self.store.note("1", "bili_dm", "", ["在考研"], now=NOW)
        groups = {g["scope"]: g["facts"] for g in self.store.pending(NOW)}
        self.assertEqual(groups, {"bili_comment": ["在考研"], "bili_dm": ["在考研"]})

    def test_expire_after_three_days_and_done_clears_one_place(self):
        self.store.note("1", "bili_comment", "", ["旧的"], now=NOW)
        self.store.note("1", "bili_dm", "", ["私信的"], now=_now(days=2))
        self.assertEqual([g["scope"] for g in self.store.pending(_now(days=3))], ["bili_dm"])
        self.store.done([("1", "bili_dm")], _now(days=3))
        self.assertEqual(self.store.pending(_now(days=3)), [])
        self.assertEqual(JsonFile(self.data / "cand.json").read({}), {})


class DigestTests(_Temp):
    def setUp(self):
        super().setUp()
        self.profiles = UserProfiles(JsonFile(self.data / "profiles.json"))
        self.store = ProfileCandidates(JsonFile(self.data / "cand.json"), lambda uid: True, lambda uid: False)
        self.prompts = []

    def run_digest(self, reply):
        async def call(prompt, max_tokens=0):
            self.prompts.append(prompt)
            return reply(prompt) if callable(reply) else reply

        return asyncio.run(profile_digest.digest_profiles(
            self.store, user_profiles=self.profiles, llm_call=call, now_ts=NOW,
        ))

    @staticmethod
    def groups_in(prompt):
        body = prompt[prompt.index('<groups trust="untrusted">') + len('<groups trust="untrusted">'):prompt.index("</groups>")]
        return json.loads(body)

    def test_sends_candidates_not_chats_and_skips_qq(self):
        self.store.note("1", "bili_comment", "挺热情", ["在学吉他"], now=NOW)
        self.store.note("1", "bili_dm", "", ["最近失眠"], now=NOW)
        self.store.note("1", "qq_private", "", ["QQ 那边的"], now=NOW)
        self.run_digest('{"users": []}')
        groups = sorted((g["where"], g["heard"]) for g in self.groups_in(self.prompts[0]))
        self.assertEqual(groups, [
            ("公开评论", {"facts": ["在学吉他"], "impressions": ["挺热情"]}),
            ("私信", {"facts": ["最近失眠"], "impressions": []}),
        ])
        self.assertEqual([g["scope"] for g in self.store.pending(NOW)], ["qq_private"])

    def test_merges_into_the_place_it_came_from_and_sees_only_that_place(self):
        self.profiles.update("1", impression="私信里很坦诚", new_facts=["在考研"], source_scope="bili_dm")
        self.profiles.update("1", username="用户1", impression="评论区的吉他人", new_facts=["会弹吉他"], new_tags=["吉他"], source_scope="bili_comment")
        self.store.note("1", "bili_comment", "", ["换了一把新吉他"], now=NOW)

        def reply(prompt):
            group = self.groups_in(prompt)[0]
            self.assertEqual(group["user"], "用户1")
            self.assertEqual(group["known"], {"impression": "评论区的吉他人", "facts": ["会弹吉他"], "tags": ["吉他"]})
            return json.dumps({"users": [{"id": "1", "impression": "爱折腾吉他的老朋友", "facts": ["换了新吉他", "会弹吉他"], "tags": ["器材党"]}]}, ensure_ascii=False)

        self.assertEqual(self.run_digest(reply), 1)
        profile = self.profiles.get("1")
        self.assertEqual(profile["impressions"], {"bili_dm": "私信里很坦诚", "bili_comment": "爱折腾吉他的老朋友"})
        self.assertEqual(profile["facts"], ["在考研", "会弹吉他", "换了新吉他"])
        self.assertEqual(profile["fact_scopes"]["在考研"], "bili_dm")
        self.assertEqual(profile["fact_scopes"]["换了新吉他"], "bili_comment")
        self.assertEqual(profile["tags"], ["吉他", "器材党"])
        self.assertEqual(self.store.pending(NOW), [])

    def test_empty_impression_keeps_the_old_one(self):
        self.profiles.update("1", impression="老印象", source_scope="bili_comment")
        self.store.note("1", "bili_comment", "", ["养了只狗"], now=NOW)
        self.run_digest(json.dumps({"users": [{"id": "1", "impression": "", "facts": ["养了只狗"], "tags": []}]}, ensure_ascii=False))
        self.assertEqual(self.profiles.get("1")["impressions"]["bili_comment"], "老印象")

    def test_malformed_batch_writes_nothing_and_keeps_candidates(self):
        self.store.note("1", "bili_comment", "", ["养了只狗"], now=NOW)
        self.assertEqual(self.run_digest('{"users": [{"id": "1", "impression": "x"}]}'), 0)
        self.assertIsNone(self.profiles.get("1"))
        self.assertEqual(len(self.store.pending(NOW)), 1)

    def test_no_call_without_candidates(self):
        self.run_digest("unused")
        self.assertEqual(self.prompts, [])

    def test_batches_are_capped_per_night(self):
        for i in range(40):
            self.store.note(str(i), "bili_comment", "", [f"第{i}个人"], now=NOW)
        self.run_digest(lambda prompt: json.dumps({"users": []}))
        self.assertEqual(len(self.prompts), profile_digest.CALLS_PER_NIGHT)
        self.assertTrue(all(len(self.groups_in(p)) == profile_digest.GROUPS_PER_CALL for p in self.prompts))
        self.assertEqual(len(self.store.pending(NOW)),
                         40 - profile_digest.CALLS_PER_NIGHT * profile_digest.GROUPS_PER_CALL)


class ClearUnreachedTests(_Temp):
    def setUp(self):
        super().setUp()
        self.profiles = UserProfiles(JsonFile(self.data / "profiles.json"))
        for uid in ("1", "2"):
            self.profiles.update(uid, username=f"用户{uid}", impression="印象", new_facts=["事实"], new_tags=["标签"],
                                 video_ref={"bvid": "BV1xx411c7mD", "relation": "uploaded_by"})
            self.profiles.record_interaction(uid, f"用户{uid}", 2, "comment")

    def test_drops_the_whole_profile(self):
        removed = self.profiles.forget(lambda uid: uid == "1")
        self.assertEqual(sorted(removed), ["2"])
        # 备份里是整条：事实、互动计数、视频关系都在
        self.assertEqual(removed["2"]["facts"], ["事实"])
        self.assertEqual(removed["2"]["relationship"]["positive_interactions"], 1)
        self.assertEqual(removed["2"]["video_refs"][0]["bvid"], "BV1xx411c7mD")
        kept = self.profiles.get("1")
        self.assertEqual((kept["impression"], kept["facts"], kept["tags"]), ("印象", ["事实"], ["标签"]))
        self.assertNotIn("2", self.profiles.all())
        self.assertEqual(self.profiles.forget(lambda uid: uid == "1"), {})

    def test_nightly_clear_backs_up_and_skips_when_affection_is_off(self):
        from test_video_memory_lifecycle import _load_consolidation_module

        saved = {}
        affection = self.affection()
        affection.set("1", 30)

        class Bot:
            user_profiles = self.profiles
            json_store = type("Store", (), {"_save_json": staticmethod(lambda path, value: saved.update({path: value}))})()

        Bot.affection = affection
        engine = _load_consolidation_module().ConsolidationEngine(Bot)
        Bot.settings = {"ENABLE_AFFECTION": False}
        self.assertEqual(engine._forget_unreached_profiles(), 0)
        self.assertEqual(self.profiles.get("2")["facts"], ["事实"])
        Bot.settings = {"ENABLE_AFFECTION": True}
        self.assertEqual(engine._forget_unreached_profiles(), 1)
        (backup,) = saved.values()
        self.assertEqual(sorted(backup), ["2"])
        self.assertEqual(self.profiles.get("1")["facts"], ["事实"])
        self.assertNotIn("2", self.profiles.all())


class ProfileDigestParserTests(unittest.TestCase):
    def test_accepts_and_dedupes(self):
        raw = json.dumps({"users": [{"id": "1", "impression": " 好人 ", "facts": ["a", "a"], "tags": []}]})
        self.assertEqual(parse_profile_digest(raw, ["1"]), {"1": {"impression": "好人", "facts": ["a"], "tags": []}})

    def test_rejects_bad_batches(self):
        good = {"id": "1", "impression": "", "facts": [], "tags": []}
        for users in (
            [{**good, "id": "9"}],
            [good, good],
            [{**good, "extra": 1}],
            [{**good, "facts": ["x"] * 6}],
            [{**good, "tags": ["长" * 13]}],
            [{**good, "facts": [""]}],
            [{**good, "impression": "长" * 81}],
            [{**good, "facts": "不是列表"}],
        ):
            with self.subTest(users=str(users)[:40]), self.assertRaises(ContentProtocolError):
                parse_profile_digest(json.dumps({"users": users}, ensure_ascii=False), ["1"])


if __name__ == "__main__":
    unittest.main()
