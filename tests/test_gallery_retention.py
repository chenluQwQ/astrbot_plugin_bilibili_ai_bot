"""图库保留规则回归：200 张上限与低权重图片 7 天自动淘汰。"""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _install_astrbot_stub():
    """不放真实 AstrBot 时也能 import core.*（与其它测试模块一致）。"""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    api = getattr(sys.modules.get("astrbot"), "api", None)
    if isinstance(api, types.ModuleType) and not getattr(api, "_logger_only", False):
        return
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.logger = _Logger()
    star = types.ModuleType("astrbot.api.star")
    data_dir = Path(tempfile.mkdtemp(prefix="bilibot-test-"))
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda _name: data_dir)
    event = types.ModuleType("astrbot.api.event")

    class _MessageChain:
        def __init__(self, *_args, **_kwargs):
            self.chain = []

        def message(self, *_args, **_kwargs):
            return self

    event.MessageChain = _MessageChain
    api.star = star
    api.event = event
    astrbot.api = api
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.star": star,
        "astrbot.api.event": event,
    })


_install_astrbot_stub()

import asyncio

from core.server_ext import image_gallery

NOW = 1_700_000_000.0
DAY = 24 * 3600


class GalleryRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bilibot-gallery-")
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(image_gallery, "DATA_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _insert(self, asset_id, created_at, *, uses=0, likes=0, status="available"):
        with image_gallery._db(create=True) as db, db:
            db.execute(
                "INSERT INTO assets(asset_id,sha256,file_name,snapshot,status,uses,likes,created_at,last_used) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (asset_id, asset_id * 2, asset_id + ".jpg", "{}", status, uses, likes, created_at, created_at),
            )

    def test_limit_is_200(self):
        self.assertEqual(image_gallery.GALLERY_LIMIT, 200)

    def test_stale_candidates_only_old_and_low_weight(self):
        old = NOW - 8 * DAY
        fresh = NOW - 1 * DAY
        self._insert("a" * 64, old, uses=0, likes=0)          # 选中：过期且零权重
        self._insert("b" * 64, old, uses=2, likes=0)          # 选中：权重 4 < 5
        self._insert("c" * 64, old, uses=3, likes=0)          # 跳过：权重 6
        self._insert("d" * 64, fresh, uses=0, likes=0)        # 跳过：未满 7 天
        self._insert("e" * 64, old, uses=0, likes=0, status="evicted")  # 跳过：已淘汰

        picked = image_gallery.stale_candidates(NOW, limit=10)

        self.assertEqual(picked, ["a" * 64, "b" * 64])

    def test_stale_candidates_respects_limit(self):
        old = NOW - 8 * DAY
        for i, ch in enumerate("abc"):
            self._insert(ch * 64, old + i)
        self.assertEqual(image_gallery.stale_candidates(NOW, limit=2), ["a" * 64, "b" * 64])

    def test_prune_stale_evicts_with_reason_and_counts_skips(self):
        old = NOW - 8 * DAY
        self._insert("a" * 64, old)
        self._insert("b" * 64, old)

        async def _evict(_bot, asset_id, *, reason="manual"):
            return {"ok": asset_id == "a" * 64, "asset_id": asset_id}

        with mock.patch.object(image_gallery, "evict_asset", side_effect=_evict) as spy:
            result = asyncio.run(image_gallery.prune_stale(object(), NOW))

        self.assertEqual(result, {"removed": 1, "skipped": 1})
        self.assertEqual([c.args[1] for c in spy.call_args_list], ["a" * 64, "b" * 64])
        self.assertTrue(all(c.kwargs["reason"] == "stale_7d" for c in spy.call_args_list))


if __name__ == "__main__":
    unittest.main()
