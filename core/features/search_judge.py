"""要不要联网搜、搜什么：调模型做的业务判断。真正去搜在 ``core/infra/search.py``。"""
import re
import json
from astrbot.api import logger
from ..infra.host_view import HostView


class SearchJudge(HostView):
    HOST_API = frozenset({"models", "settings"})

    async def _should_search_for_video(self, video_info: dict, extra_context: str) -> str:
        title = video_info.get("title", "")
        desc = video_info.get("desc", "")[:200]
        tname = video_info.get("tname", "")
        owner = video_info.get("owner_name") or video_info.get("up_name", "")
        prompt = f"""判断以下B站视频是否需要联网搜索来补充背景知识，以便更好地理解视频内容。

视频标题：{title}
UP主：{owner}
分区：{tname}
简介：{desc}
{extra_context[:300] if extra_context else ''}

以下情况需要搜索：涉及时事新闻、专业领域知识、特定人物/事件/产品、最新科技动态、争议性话题等。
以下情况不需要搜索：日常vlog、搞笑视频、纯娱乐内容、游戏实况、个人分享等。

请用JSON回复：{{"need_search": true或false, "query": "搜索关键词(不需要搜索则留空)"}}
直接输出JSON。"""
        try:
            text = await self.models._llm_call(prompt, max_tokens=100)
            if not text:
                return ""
            text = text.replace("```json", "").replace("```", "").strip()
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                obj = json.loads(m.group())
                if obj.get("need_search"):
                    return (obj.get("query") or title).strip()
            return ""
        except Exception as e:
            logger.debug(f"[BiliBot] 搜索判断失败: {e}")
            return ""

    async def _should_search_for_reply(self, user_comment: str, context: str = "") -> str:
        if not self.settings.get("ENABLE_WEB_SEARCH"):
            return ""
        if not self.settings.get("WEB_SEARCH_API_KEY"):
            return ""
        stripped = re.sub(r'\[.*?\]', '', user_comment).strip()
        if len(stripped) < 4:
            return ""
        SKIP_PATTERNS = (
            "哈哈", "hh", "笑死", "666", "好的", "谢谢", "感谢", "ok", "嗯嗯",
            "确实", "真的", "是的", "对的", "可以", "不错", "厉害", "牛", "绝了",
            "啊这", "草", "乐", "蚌", "典", "急了", "麻了", "顶", "dd", "催更",
            "前排", "火钳刘明", "来了", "打卡", "支持", "加油", "冲", "爱了",
        )
        if stripped.lower() in SKIP_PATTERNS or all(c in "。，！？~…、哈呵嘿嗯啊哦呀w～" for c in stripped):
            return ""
        judge_provider = self.settings.get("WEB_SEARCH_JUDGE_PROVIDER_ID")
        ctx_block = f"\n最近对话上下文：\n{context[:500]}\n" if context else ""
        prompt = f"""判断以下B站用户评论是否需要联网搜索才能准确回复。
{ctx_block}
用户最新评论：「{user_comment[:300]}」

需要搜索的情况：用户提问了某个事实性问题、问了近期新闻/事件、提到了你可能不了解的专业知识/人物/产品/梗、要求你查某些信息。
不需要搜索的情况：日常聊天、打招呼、表情、吐槽、纯情感表达、闲聊、你能凭自身知识回答的内容。

请用JSON回复：{{"need_search": true或false, "query": "搜索关键词(不需要搜索则留空)"}}
直接输出JSON，不要加任何其他内容。"""
        try:
            text = await self.models._llm_call(prompt, max_tokens=80, provider_id=judge_provider or None)
            if not text:
                return ""
            text = text.replace("```json", "").replace("```", "").strip()
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                obj = json.loads(m.group())
                if obj.get("need_search"):
                    query = (obj.get("query") or "").strip()
                    if query:
                        logger.info(f"[BiliBot] 🔍 评论触发联网搜索: {query[:60]}")
                        return query
            return ""
        except Exception as e:
            logger.debug(f"[BiliBot] 评论搜索判断失败: {e}")
            return ""
