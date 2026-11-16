"""记忆向量可以用 AstrBot 里配好的向量模型：填了 ID 就走它，出错、维度不对都跳过这次向量检索。"""
import asyncio
import types
import unittest

from core.infra.vision import Vision
from core.settings import Settings, with_schema_defaults


class _Provider:
    def __init__(self, vector, dim=0, error=None):
        self.vector, self.dim, self.error = vector, dim, error
        self.texts = []

    async def get_embedding(self, text):
        self.texts.append(text)
        if self.error:
            raise self.error
        return self.vector

    def get_dim(self):
        return self.dim


def vision(overrides, providers):
    config = with_schema_defaults(overrides)
    host = types.SimpleNamespace(
        config=config, settings=Settings(config),
        context=types.SimpleNamespace(get_provider_by_id=providers.get),
        _embed_client=None,
    )
    return Vision(host)


class HostEmbeddingTest(unittest.TestCase):
    def test_uses_host_provider_when_configured(self):
        provider = _Provider([0.1, 0.2, 0.3], dim=3)
        v = vision({"EMBED_PROVIDER_ID": "emb", "EMBED_API_KEY": "unused"}, {"emb": provider})
        self.assertEqual(asyncio.run(v._get_embedding("你好")), [0.1, 0.2, 0.3])
        self.assertEqual(provider.texts, ["你好"])

    def test_zero_dim_is_not_checked(self):
        v = vision({"EMBED_PROVIDER_ID": "emb"}, {"emb": _Provider([0.5, 0.5], dim=0)})
        self.assertEqual(asyncio.run(v._get_embedding("x")), [0.5, 0.5])

    def test_wrong_dim_missing_provider_and_errors_skip(self):
        cases = [
            {"emb": _Provider([0.1, 0.2], dim=3)},
            {},
            {"emb": _Provider(None, error=RuntimeError("boom"))},
            {"emb": _Provider([])},
        ]
        for providers in cases:
            v = vision({"EMBED_PROVIDER_ID": "emb"}, providers)
            self.assertIsNone(asyncio.run(v._get_embedding("x")))

    def test_empty_id_keeps_old_path(self):
        provider = _Provider([1.0])
        v = vision({"EMBED_PROVIDER_ID": "", "EMBED_API_KEY": ""}, {"emb": provider})
        self.assertIsNone(asyncio.run(v._get_embedding("x")))
        self.assertEqual(provider.texts, [])


if __name__ == "__main__":
    unittest.main()
