"""测试里替掉发动态的「跑一轮」和「收想法」，其余（交接时查开关、池子、排队）用真的 ``DynamicPost``。

假插件上写 ``_run_dynamic`` / ``_collect_dynamic_idea`` 记录调用，再挂 ``dynamic_post = part(HostRecordedPost)``。
"""
from core.features.dynamic_post import DynamicPost


class HostRecordedPost(DynamicPost):
    async def run(self, human_initiated=False, fresh=()):
        return await self._host._run_dynamic(fresh=fresh)

    def collect_idea(self, **idea):
        return self._host._collect_dynamic_idea(**idea)
