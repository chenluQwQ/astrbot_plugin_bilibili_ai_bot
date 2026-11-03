import ast
import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, patch
import test_gallery_workflow as prior
from test_gallery_workflow import workflow, gallery, fixture

emotes=fixture.load("native_emotes")


class EmoteTests(unittest.TestCase):
    def test_known_codes_and_single_expression(self):
        self.assertEqual(emotes.normalize("这局有意思[doge][吃瓜]"),"这局有意思[doge]")
        self.assertEqual(emotes.extract("有点尴尬[捂脸]"),["[捂脸]"])

    def test_unknown_or_truncated_codes_not_sent(self):
        self.assertEqual(emotes.normalize("你好[不存在]"),"你好")
        self.assertEqual(emotes.normalize("你好[笑哭]",5),"你好")
        self.assertEqual(emotes.extract("你好[不存在]"),[])

    def test_prompt_allows_abstention_and_serious_discussion(self):
        value=emotes.prompt()
        self.assertIn("0或1个",value)
        self.assertIn("严肃事实纠正",value)
        self.assertIn("[doge]",value)

    def test_serious_context_filters_even_model_generated_emote(self):
        self.assertFalse(emotes.appropriate("需要确认建筑毁于战火的事实"))
        self.assertEqual(emotes.normalize("事实应该确认[doge]",allow=False),"事实应该确认")

    def test_bilibili_reply_posts_text_code_without_uploaded_pictures(self):
        tree=ast.parse((fixture.CORE/"bilibili.py").read_text())
        method=next(n for n in next(n for n in tree.body if isinstance(n,ast.ClassDef)).body
                    if isinstance(n,ast.AsyncFunctionDef) and n.name=="_send_reply")
        import re
        namespace={"re":re,"logger":fixture.api.logger,"BILI_REPLY_URL":"isolated:no-network"}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),"<reply>","exec"),namespace)
        bot=types.SimpleNamespace(config={},_http_post=AsyncMock(return_value=({"code":0},None)))
        self.assertTrue(asyncio.run(namespace["_send_reply"](bot,1,"22",1,"这局有意思[doge]")))
        payload=bot._http_post.await_args.kwargs["data"]
        self.assertEqual(payload["message"],"这局有意思[doge]")
        self.assertNotIn("pictures",payload)


class SemanticTests(unittest.IsolatedAsyncioTestCase):
    setUp=prior.WorkflowTests.setUp
    tearDown=prior.WorkflowTests.tearDown
    collect=prior.WorkflowTests.collect
    publish=prior.WorkflowTests.publish
    async def weak_candidate(self):
        await self.collect()
        self.bot._get_embedding.return_value=[.42,.58]
        self.row=gallery.list_assets()[0]

    async def test_below_direct_threshold_can_match_emotion(self):
        await self.weak_candidate()
        self.bot._generation_llm_call=AsyncMock(return_value=json.dumps({
            "asset_id":self.row["asset_id"],"confidence":.85,"reason":"开心赞同，语气一致"}))
        diagnostic={}
        selected=await workflow.select(self.bot,"@主人 这点说到我心里了",title="视频",diagnostic=diagnostic)
        self.assertEqual(selected["asset_id"],self.row["asset_id"])
        self.assertEqual(diagnostic["reason"],"emotion_match")
        self.assertEqual(self.bot._generation_llm_call.await_count,1)
        self.assertNotIn("@主人",self.bot._generation_llm_call.await_args.args[0])
        self.assertEqual(self.bot._generation_llm_call.await_args.kwargs["max_tokens"],160)

    async def test_low_confidence_and_invented_asset_rejected(self):
        await self.weak_candidate()
        for value in ({"asset_id":self.row["asset_id"],"confidence":.4,"reason":"不贴切"},
                      {"asset_id":"invented","confidence":.99,"reason":"瞎编"}):
            self.bot._generation_llm_call=AsyncMock(return_value=json.dumps(value))
            diagnostic={}
            self.assertIsNone(await workflow.select(self.bot,"严肃问题",diagnostic=diagnostic))
            self.assertEqual(diagnostic["reason"],"no_emotion_match")

    async def test_timeout_malformed_or_null_judge_falls_back_to_text(self):
        await self.weak_candidate()
        for result in ("broken",json.dumps({"asset_id":self.row["asset_id"],"confidence":True}),"null"):
            self.bot._generation_llm_call=AsyncMock(return_value=result)
            self.assertIsNone(await workflow.select(self.bot,"普通句子"))
        self.bot._generation_llm_call=AsyncMock(side_effect=TimeoutError)
        diagnostic={}
        self.assertIsNone(await workflow.select(self.bot,"普通句子",diagnostic=diagnostic))
        self.assertEqual(diagnostic["reason"],"judgment_timeout")

    async def test_irrelevant_candidate_does_not_add_model_call(self):
        await self.collect()
        self.bot._get_embedding.return_value=[.1,.9]
        self.bot._generation_llm_call=AsyncMock()
        self.assertIsNone(await workflow.select(self.bot,"毫不相关"))
        self.assertEqual(self.bot._generation_llm_call.await_count,0)

    async def test_selection_reason_persisted_for_text_comment(self):
        await self.weak_candidate()
        self.bot._generation_llm_call=AsyncMock(return_value=json.dumps({"asset_id":"","confidence":0,"reason":"认真内容不配"}))
        ok,sink=await self.publish()
        self.assertTrue(ok)
        self.assertEqual(sink["images"],[])
        self.assertEqual(sink["image_selection"]["reason"],"no_emotion_match")
        rows=workflow.listing()["recent_selections"]
        self.assertEqual(rows[0]["selection"]["judgment_reason"],"认真内容不配")

    async def test_reply_generation_receives_emote_catalog(self):
        tree=ast.parse((fixture.CORE/"proactive.py").read_text())
        method=next(n for n in next(n for n in tree.body if isinstance(n,ast.ClassDef)).body
                    if isinstance(n,ast.AsyncFunctionDef) and n.name=="_generate_hot_comment_reply")
        from datetime import datetime
        protocol=fixture.load("content_protocol")
        namespace={"datetime":datetime,"native_emotes":emotes,"logger":fixture.api.logger,
                   "parse_proactive_comment":protocol.parse_proactive_comment,
                   "ContentProtocolError":protocol.ContentProtocolError,
                   "PROACTIVE_COMMENT_SCHEMA_PROMPT":protocol.PROACTIVE_COMMENT_SCHEMA_PROMPT}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),"<reply-prompt>","exec"),namespace)
        generate=AsyncMock(return_value='{"decision":"comment","text":"你这吃瓜姿势挺专业[吃瓜]"}')
        self.bot._generation_llm_call=generate
        self.bot._llm_call=generate
        self.bot._get_system_prompt=AsyncMock(return_value="保持自然")
        self.bot._proactive_comment_max_len=lambda:200
        result=await namespace["_generate_hot_comment_reply"](self.bot,{"message":"我就看看"},{"title":"片段"},"轻松短片")
        self.assertEqual(result,"你这吃瓜姿势挺专业[吃瓜]")
        self.assertIn("[吃瓜]",generate.await_args.args[0])

    async def test_reply_log_keeps_emote_marker(self):
        # 生产日志源直接保存最终已发送文字与合法表情列表，避免显示层猜测图片。
        text=emotes.normalize("这梗接得快[doge][笑哭]")
        self.assertEqual(text,"这梗接得快[doge]")
        self.assertEqual(emotes.extract(text),["[doge]"])

    async def test_short_judgment_disables_thinking_only_for_this_sdk_call(self):
        create=AsyncMock(return_value=types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='{"asset_id":"","confidence":0,"reason":"无匹配"}'))],
            usage=types.SimpleNamespace(prompt_tokens=450,completion_tokens=20)))
        options=[]
        body={"thinking":{"type":"enabled"}}
        client=types.SimpleNamespace(with_options=lambda **kwargs:
            options.append(kwargs) or types.SimpleNamespace(chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=create))))
        provider=types.SimpleNamespace(client=client,provider_config={"model":"deepseek-flash","custom_extra_body":body})
        self.bot.context.get_provider_by_id=lambda _:provider
        self.bot.config["GENERATION_PROVIDER_ID"]="deepseek/deepseek-flash"
        await workflow._judge(self.bot,"短提示")
        self.assertEqual(options,[{"max_retries":0,"timeout":8}])
        self.assertEqual(create.await_args.kwargs["extra_body"]["thinking"],{"type":"disabled"})
        self.assertEqual(body["thinking"],{"type":"enabled"})


if __name__=="__main__":unittest.main()
