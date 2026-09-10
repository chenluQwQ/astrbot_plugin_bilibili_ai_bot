"""Bounded search diversity; no extra model calls or persistent preferences."""
import random
import re
from difflib import SequenceMatcher


# Concrete starting points for exploration, not a permanent interest profile.
EXPLORATION_QUERIES = (
    "独立游戏 开发幕后", "手工 木工制作", "科学实验 原理演示",
    "地方美食 制作过程", "历史文物 修复", "乐器演奏 幕后",
    "运动技巧 入门", "动画制作 幕后", "开源软件 实用技巧",
    "动物行为 科普", "城市建筑 设计", "电影 实拍特效",
)
# Common wording variants supplement literal similarity. Unknown topics still
# use text similarity; this is a repetition heuristic, not semantic recall.
_TOPIC_ALIASES = (
    ("白噪", "助眠", "环境音", "雨声", "鲸鸣", "鲸鱼叫声", "asmr"),
    ("深海", "深渊生物", "海洋", "海底", "水下"),
    ("极光", "星轨", "星空", "夜空"),
    ("游戏", "联机"), ("木工", "手工"), ("科学实验", "物理实验"),
    ("美食", "烹饪", "烘焙"), ("历史", "文物"), ("乐器", "演奏"),
    ("运动", "健身"), ("动画", "动漫"), ("软件", "编程"),
    ("动物", "宠物"), ("建筑", "城市规划"), ("电影", "特效"),
)


def _normalized(text):
    return re.sub(r"[\W_]+", "", str(text or "").lower())[:240]


def similar_topic(left, right):
    left, right = _normalized(left), _normalized(right)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    if any(any(word in left for word in group) and
           any(word in right for word in group) for group in _TOPIC_ALIASES):
        return True
    return SequenceMatcher(None, left, right).ratio() >= 0.65


def _entry_topic(entry):
    # The actual search/title is exposure evidence. Model-suggested next search
    # words are deliberately excluded: proposing a word does not mean seeing it.
    query = entry.get("source_detail") if entry.get("source") == "search" else ""
    return str(query or entry.get("title") or "")[:240]


def prepare_discovery_queries(proposed, history):
    """Keep familiar interests; sometimes bring a fresh direction to the front."""
    entries = [item for item in history if isinstance(item, dict)]
    recent_topics = [_entry_topic(item) for item in entries[-12:]]
    recent_searches = [item for item in entries if item.get("source") == "search"][-2:]
    explore_first = len(recent_searches) == 2 and not any(
        item.get("discovery_mode") == "explore" for item in recent_searches
    ) and random.random() < 0.35

    def exposure(query):
        return sum(similar_topic(query, topic) for topic in recent_topics)

    seeds = list(EXPLORATION_QUERIES)
    random.shuffle(seeds)
    seeds.sort(key=exposure)
    exploration = seeds[0]
    chosen = []
    for query in sorted(proposed, key=exposure):
        if not query or similar_topic(query, exploration):
            continue
        if any(similar_topic(query, previous) for previous in chosen):
            continue
        # Repetition lowers ordering, never bans a liked subject.
        chosen.append(query)
        if len(chosen) == 2:
            break
    for seed in seeds[1:]:
        if len(chosen) >= 2:
            break
        if not similar_topic(seed, exploration) and not any(
            similar_topic(seed, previous) for previous in chosen
        ):
            chosen.append(seed)
    return ([exploration] + chosen if explore_first else chosen + [exploration])[:3]
