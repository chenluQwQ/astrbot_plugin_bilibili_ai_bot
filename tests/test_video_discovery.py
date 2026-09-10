import unittest
from unittest.mock import patch

from core.video_discovery import (
    EXPLORATION_QUERIES, prepare_discovery_queries, similar_topic,
)


class VideoDiscoveryTests(unittest.TestCase):
    def test_repeated_noise_and_ocean_do_not_fill_all_queries(self):
        history = [
            {"source": "search", "source_detail": query}
            for query in ("深海白噪音", "真实雨声助眠", "深海生物纪录片")
        ]
        result = prepare_discovery_queries(
            ["鲸鸣助眠", "海底生物", "深海白噪音"], history,
        )
        self.assertEqual(len(result), 3)
        self.assertTrue(any(query in EXPLORATION_QUERIES for query in result))
        self.assertLess(sum(similar_topic(query, "深海白噪音") for query in result), 3)

    @patch("core.video_discovery.random.random", return_value=0.2)
    def test_exploration_can_move_forward_based_on_persisted_history(self, _random):
        history = [{"source": "search", "source_detail": "冷门语言学"}] * 200
        modes = []
        for _ in range(12):
            # No per-process state: persisted capped history drives the cadence.
            result = prepare_discovery_queries(["冷门语言学", "数学悖论"], history)
            mode = "explore" if result[0] in EXPLORATION_QUERIES else "interest"
            modes.append(mode)
            history = (history + [{
                "source": "search", "source_detail": result[0],
                "discovery_mode": mode,
            }])[-200:]
        for start in range(len(modes) - 2):
            self.assertIn("explore", modes[start:start + 3])

    @patch("core.video_discovery.random.random", return_value=0.9)
    def test_exploration_is_not_a_mandatory_quota(self, _random):
        history = [{"source": "search", "source_detail": "深海生物"}] * 12
        result = prepare_discovery_queries(["深海白噪音", "极光"], history)
        self.assertEqual(result[:2], ["极光", "深海白噪音"])
        self.assertIn(result[2], EXPLORATION_QUERIES)

    def test_unseen_model_interest_remains_available(self):
        result = prepare_discovery_queries(["冷门语言学", "数学悖论"], [])
        self.assertEqual(result[:2], ["冷门语言学", "数学悖论"])
        self.assertIn(result[2], EXPLORATION_QUERIES)

    def test_no_model_queries_still_produce_three_different_directions(self):
        result = prepare_discovery_queries([], [])
        self.assertEqual(len(result), 3)
        for index, query in enumerate(result):
            self.assertTrue(all(not similar_topic(query, other) for other in result[index + 1:]))

    def test_suggested_but_unwatched_words_are_not_counted_as_exposure(self):
        history = [{"title": "数学悖论", "search_keywords": ["冷门语言学"]}] * 4
        self.assertIn("冷门语言学", prepare_discovery_queries(["冷门语言学"], history))

    def test_followed_video_titles_also_count_as_recent_exposure(self):
        history = [{
            "source": "follow", "source_detail": "following", "title": "深海生物纪录片",
        }] * 2
        result = prepare_discovery_queries(["海底生物", "冷门语言学"], history)
        self.assertEqual(result[:2], ["冷门语言学", "海底生物"])
