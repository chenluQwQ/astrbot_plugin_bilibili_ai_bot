"""和插件业务无关的文本处理函数。"""

import json
import re
from urllib.parse import urlsplit


def normalize_openai_base_url(value):
    """Append /v1 only to a bare host URL; a URL that already has a path is kept as-is."""
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    parsed = urlsplit(url if "://" in url else f"//{url}")
    if parsed.path.strip("/"):
        return url
    return f"{url}/v1"


def repair_llm_json(text):
    """修复LLM返回的各种JSON格式问题"""
    # 去 markdown 包裹
    text = text.replace("```json", "").replace("```", "").strip()
    # 中文引号 → 安全字符
    text = text.replace('“', "'").replace('”', "'")
    text = text.replace('‘', "'").replace('’', "'")
    text = text.replace('＂', "'")  # 全角双引号
    # 去掉尾逗号
    text = re.sub(r',\s*([}\]])', r'\1', text)
    # 从每个括号位置尝试完整解析，取第一个合法的 JSON 对象/数组；
    # 既支持顶层数组，也不会把正文里的「[说明]」误当 JSON（解析失败会继续向后找）
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch in "[{":
            try:
                obj, end = decoder.raw_decode(text[idx:])
            except ValueError:
                continue
            if isinstance(obj, dict):
                return text[idx:idx + end]
            # 数组只接受对象列表（如批量评估结果），跳过正文里的 [1,2]、[说明] 等
            if isinstance(obj, list) and all(isinstance(x, dict) for x in obj):
                return text[idx:idx + end]
    return text


def extract_json_object(text):
    """从模型回复里抠出一个 JSON 对象（每日计划、关注动态巡视在用）；抠不出来返回空字典。"""
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S)
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        try:
            value = json.loads(repair_llm_json(raw))
            if isinstance(value, dict):
                return value
        except Exception:
            pass
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
