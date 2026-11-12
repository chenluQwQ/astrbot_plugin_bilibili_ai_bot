"""Model fields in the Web config page are dropdowns of AstrBot's providers."""

import types
import unittest

import test_webui_interest  # noqa: F401  装假的 astrbot
from core.web import config_page


def provider(provider_id, model=""):
    return types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id=provider_id, model=model))


def plugin_with(*providers):
    return types.SimpleNamespace(
        context=types.SimpleNamespace(get_all_providers=lambda: list(providers))
    )


class ProviderOptionTests(unittest.TestCase):
    def test_every_provider_field_lists_astrbot_providers(self):
        schema = config_page._schema_with_choices(
            plugin_with(provider("main", "gpt-x"), provider("vision", "vision"))
        )
        provider_keys = [k for k, f in schema.items() if f.get("_special") == "select_provider"]
        self.assertIn("LLM_PROVIDER_ID", provider_keys)
        for key in provider_keys:
            self.assertEqual(schema[key]["options"], ["", "main", "vision"])
            self.assertEqual(schema[key]["option_labels"][""], "跟随 AstrBot 默认")
            # 下拉只展示模型名；provider id 仍是保存的值。
            self.assertEqual(schema[key]["option_labels"]["main"], "gpt-x")
            self.assertEqual(schema[key]["option_labels"]["vision"], "vision")

    def test_duplicate_provider_ids_are_listed_once(self):
        schema = config_page._schema_with_choices(
            plugin_with(provider("main", "gpt-x"), provider("main", "gpt-y"), provider("vision", "vision"))
        )
        self.assertEqual(schema["LLM_PROVIDER_ID"]["options"], ["", "main", "vision"])
        self.assertEqual(schema["LLM_PROVIDER_ID"]["option_labels"]["main"], "gpt-x")

    def test_without_providers_fields_stay_free_text(self):
        schema = config_page._schema_with_choices(plugin_with())
        self.assertNotIn("options", schema["LLM_PROVIDER_ID"])

    def test_saving_a_provider_missing_from_the_list_is_still_allowed(self):
        # Saves validate against the static schema, not the live provider list.
        self.assertNotIn("options", config_page._load_schema()["LLM_PROVIDER_ID"])


if __name__ == "__main__":
    unittest.main()
