"""知识库查询工具测试：白名单、防碰撞、归属复核、未配置停用。"""
import asyncio
import types
import unittest

from core.infra.knowledge import KnowledgeLookup, authorized_kbs
from core.settings import Settings, with_schema_defaults


class _Config:
    def __init__(self, settings):
        object.__setattr__(self, "_settings", settings)

    def __getattr__(self, name):
        raise AttributeError(name)


class _KB:
    def __init__(self, kb_id, kb_name, init_error=None):
        self.kb = types.SimpleNamespace(kb_id=kb_id, kb_name=kb_name)
        self.init_error = init_error


class _Mgr:
    def __init__(self, helpers):
        self._helpers = helpers

    async def get_kb(self, kb_id):
        return self._helpers.get(kb_id)

    async def get_kb_by_name(self, name):
        # 模拟宿主行为：先按名称，再按 UUID 兜底。
        for h in self._helpers.values():
            if h.kb.kb_name == name:
                return h
        return self._helpers.get(name)


class AuthorizedKbsTests(unittest.TestCase):
    def test_returns_authorized_ids_only(self):
        mgr = _Mgr({"kb-1": _KB("kb-1", "库一")})
        result = asyncio.run(authorized_kbs(mgr, ["kb-1", "kb-missing"]))
        self.assertEqual(result, {"kb-1": "库一"})

    def test_rejects_uuid_used_as_other_kb_name(self):
        # 另一资料库拿授权 UUID 当名称：交叉复核必须拒绝。
        impostor = _KB("kb-impostor", "kb-1")
        mgr = _Mgr({"kb-impostor": impostor})
        result = asyncio.run(authorized_kbs(mgr, ["kb-1"]))
        self.assertEqual(result, {})

    def test_skips_errored_helper(self):
        mgr = _Mgr({"kb-1": _KB("kb-1", "库一", init_error="boom")})
        result = asyncio.run(authorized_kbs(mgr, ["kb-1"]))
        self.assertEqual(result, {})

    def test_caps_at_three_kbs(self):
        helpers = {f"kb-{i}": _KB(f"kb-{i}", f"库{i}") for i in range(5)}
        mgr = _Mgr(helpers)
        result = asyncio.run(authorized_kbs(mgr, [f"kb-{i}" for i in range(5)]))
        self.assertEqual(len(result), 3)


class KnowledgeLookupTests(unittest.TestCase):
    def _lookup(self, config_overrides, kb_manager=None):
        config = with_schema_defaults(config_overrides)
        settings = Settings(config)
        view = KnowledgeLookup(None)
        object.__setattr__(view, "_host", types.SimpleNamespace(
            settings=settings, context=types.SimpleNamespace(kb_manager=kb_manager),
        ))
        return view

    def test_empty_ids_stops(self):
        view = self._lookup({"KB_LOOKUP_IDS": []})
        self.assertEqual(asyncio.run(view.lookup("任意")), "")

    def test_missing_kb_manager_stops(self):
        view = self._lookup({"KB_LOOKUP_IDS": ["kb-1"]}, kb_manager=None)
        self.assertEqual(asyncio.run(view.lookup("任意")), "")

    def test_lookup_returns_joined_chunks(self):
        mgr = _Mgr({"kb-1": _KB("kb-1", "库一")})

        async def retrieve(query, kb_names, top_k_fusion, top_m_final):
            return {"results": [
                {"kb_id": "kb-1", "doc_id": "d", "chunk_id": "c",
                 "content": "设定内容", "kb_name": "库一", "doc_name": "设定.md"},
                {"kb_id": "kb-别的", "doc_id": "d2", "chunk_id": "c2",
                 "content": "白名单外，必须被丢弃", "kb_name": "x", "doc_name": "y"},
            ]}

        mgr.retrieve = retrieve
        view = self._lookup({"KB_LOOKUP_IDS": ["kb-1"]}, kb_manager=mgr)
        text = asyncio.run(view.lookup("查一下设定"))
        self.assertIn("设定内容", text)
        self.assertNotIn("白名单外", text)

    def test_no_hits_returns_empty(self):
        mgr = _Mgr({"kb-1": _KB("kb-1", "库一")})

        async def retrieve(query, kb_names, top_k_fusion, top_m_final):
            return {"results": []}

        mgr.retrieve = retrieve
        view = self._lookup({"KB_LOOKUP_IDS": ["kb-1"]}, kb_manager=mgr)
        self.assertEqual(asyncio.run(view.lookup("查不到")), "")


if __name__ == "__main__":
    unittest.main()
