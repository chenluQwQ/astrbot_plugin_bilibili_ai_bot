"""主干：Bot 的一天。

一天的状态（睡眠 / 清醒 / 日终清算）、每个活动的状态机、唯一的主循环、
分支报告的事件。主干只认识「分支」这个接口，不知道具体有哪些分支；
只能 import lifecycle、storage、settings（tests/test_import_direction.py 检查）。
"""
