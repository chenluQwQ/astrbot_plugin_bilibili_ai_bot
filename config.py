"""性格演化系统：每日反思、说话习惯、看法变化。"""
import re
import json
import asyncio
from datetime import datetime
from astrbot.api import logger
from .config import PERSONALITY_FILE


class PersonalityMixin:
    """性格演化。"""

    def _get_personality_prompt(self):
        evo = self._load_json(PERSONALITY_FILE, {})
        if not evo:
            return ""
        parts = []
        traits = evo.get("evolved_traits", [])
        if traits:
            parts.append("【最近的成长变化】")
            for t in traits[-3:]:
                parts.append(f"- {t['change']}")
        habits = evo.get("speech_habits", [])
        if habits:
            parts.append("【当前说话习惯】" + "；".join(habits))
        opinions = evo.get("opinions", [])
        if opinions:
            parts.append("【对事物的看法】" + "；".join(opinions))
        return "\n".join(parts) if parts else ""

    @staticmethod
    def _parse_evolve_json(raw_text, old_habits, old_opinions):
        text = raw_text.replace("```json", "").replace("```", "").strip()
        # 修复LLM返回的中文引号导致JSON解析失败
        text = text.replace('\u201c', "'").replace('\u201d', "'").replace('\u2018', "'").replace('\u2019', "'")
        try:
            return json.loads(text)
        except Exception:
            pass
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
        json_start = text.find('{')
        if json_start != -1:
            fragment = text[json_start:]
            ob = fragment.count('{') - fragment.count('}')
            oq = fragment.count('[') - fragment.count(']')
            fragment = re.sub(r',?\s*"[^"]*$', '', fragment)
            fragment = re.sub(r',\s*$', '', fragment)
            fragment += ']' * max(0, oq) + '}' * max(0, ob)
            try:
                return json.loads(fragment)
            except Exception:
                pass
        logger.warning(f"[BiliBot] 性格演化JSON解析失败：{raw_text[:200]}")
        reflection = ""
        rm = re.search(r'"reflection"\s*:\s*"([^"]*)"', text)
        if rm:
            reflection = rm.group(1)
        return {
            "new_trait": "", "trigger": "",
            "speech_habits": old_habits, "opinions": old_opinions,
            "reflection": reflection or "今天的反思没能整理好...",
        }

    async def _maybe_evolve_personality(self):
        if not self.config.get("ENABLE_PERSONALITY_EVOLUTION", True):
            return
        evo = self._load_json(PERSONALITY_FILE, {})
        today = datetime.now().strftime("%Y-%m-%d")
        if evo.get("last_evolve", "")[:10] == today:
            return
        evolve_hour = self.config.get("EVOLVE_HOUR", 1)
        if datetime.now().hour != evolve_hour:
            return
        logger.info("[BiliBot] 🌱 开始每日性格演化反思...")
        recent = sorted(self._memory, key=lambda x: x.get("time", ""), reverse=True)[:30]
        if len(recent) < 5:
            logger.info("[BiliBot] 🌱 记忆太少，跳过演化")
            return
        recent_texts = "\n".join([m["text"] for m in recent[:20]])
        old_traits = evo.get("evolved_traits", [])
        old_habits = evo.get("speech_habits", [])
        old_opinions = evo.get("opinions", [])
        sp = self._get_system_prompt()
        on = self.config.get("OWNER_NAME", "") or "主人"
        default_evolve_prompt = """现在是睡前反思时间。请根据你最近的互动经历，思考自己有没有发生什么变化。

【之前已经发生的变化】
{old_traits}

【当前说话习惯】
{old_habits}

【当前对事物的看法】
{old_opinions}

【最近的互动记录】
{recent_texts}

请思考：
1. 最近的经历有没有让你的语气或说话方式产生微妙变化？
2. 有没有形成新的说话习惯？
3. 对什么事物产生了新的看法？

注意：变化应该是微妙的、渐进的，不要突变。如果没什么变化就如实说。

请以JSON格式回复：
{{"new_trait": "新的变化描述（没有就留空）", "trigger": "什么触发了这个变化", "speech_habits": ["当前所有说话习惯，含旧的，最多5条"], "opinions": ["当前所有看法，含旧的，最多5条"], "reflection": "一句话的睡前感想"}}"""
        custom_prompt = self.config.get("EVOLVE_PROMPT", "").strip()
        tpl = custom_prompt if custom_prompt else default_evolve_prompt
        prompt = tpl.format(
            old_traits=json.dumps(old_traits[-5:], ensure_ascii=False) if old_traits else "暂无",
            old_habits=json.dumps(old_habits, ensure_ascii=False) if old_habits else "暂无",
            old_opinions=json.dumps(old_opinions, ensure_ascii=False) if old_opinions else "暂无",
            recent_texts=recent_texts,
            owner_name=on,
        )
        max_retries = self.config.get("EVOLVE_MAX_RETRIES", 2)
        for attempt in range(max_retries):
            try:
                text = await self._llm_call(prompt, system_prompt=sp, max_tokens=1024)
                if not text:
                    raise ValueError("LLM返回空")
                result = self._parse_evolve_json(text, old_habits, old_opinions)
                if not result.get("new_trait") and result.get("reflection") == "今天的反思没能整理好...":
                    raise ValueError(f"JSON解析兜底：{text[:100]}")
                new_trait = result.get("new_trait", "")
                if new_trait:
                    old_traits.append({"time": today, "change": new_trait, "trigger": result.get("trigger", "")})
                    old_traits = old_traits[-10:]
                evo = {
                    "version": evo.get("version", 0) + 1,
                    "last_evolve": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "evolved_traits": old_traits,
                    "speech_habits": result.get("speech_habits", old_habits)[-5:],
                    "opinions": result.get("opinions", old_opinions)[-5:],
                    "last_reflection": result.get("reflection", ""),
                }
                self._save_json(PERSONALITY_FILE, evo)
                if new_trait:
                    logger.info(f"[BiliBot] 🌱 性格演化：{new_trait}")
                else:
                    logger.info("[BiliBot] 🌱 今日无明显变化")
                logger.info(f"[BiliBot] 🌱 反思：{result.get('reflection', '')}")
                return
            except Exception as e:
                logger.warning(f"[BiliBot] 性格演化失败（第{attempt + 1}/{max_retries}次）：{e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(30)
        evo["last_evolve"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._save_json(PERSONALITY_FILE, evo)
        logger.error(f"[BiliBot] 🌱 性格演化连续{max_retries}次失败，今日跳过")
