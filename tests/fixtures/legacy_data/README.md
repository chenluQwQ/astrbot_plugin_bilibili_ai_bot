# 旧数据样例

一份真实运行过的 `data/` 目录，脱敏后每个文件留几条。给搬数据、写导入器的改动当基准，测试在 `tests/test_legacy_data.py`。

## 脱敏规则

- 同一个原值永远换成同一个假值，键和值一起换，所以跨文件的引用还对得上（记忆里的用户 ↔ 用户画像 ↔ 好感度，看片记录 ↔ 看过的视频，事实列表 ↔ `fact_scopes` 的键）
- 自由文本换成「假N样例文本…」，**长度不变**（字段有字数上限）
- UID、rpid、动态 id 等换成同样位数的随机数字；BV 号换成随机 BV 号；链接换成 `example.invalid`
- 原样保留：代码会拿来判断的枚举值（`source`、`memory_type`、`scope`、`mood`、`stage`……）、时间、分区名、数字、布尔值、1–3 位的短数字
- 向量换成同样维数的随机数
- 列表和按 id 存的字典先挑出能覆盖各种枚举值的记录，再补到十几条

生成时检查过：输出里没有任何原文（4 字以上）、原始 id 或原始 BV 号。生成脚本会读真实数据，所以不放进仓库。

## 没有放进来的

- `bilibot.sqlite3`：记忆和看过的视频在 json 里也有一份，启动迁移从 json 导入
- `backup/`、`share_send_videos/`、临时图片和视频
- `memory_cleanup_backup_*.json`（旧版清理记忆时留下的备份）

## 坏文件

`../legacy_data_broken/` 是手工做的，覆盖在正常样例上用：截断的 `memory.json`、空的 `user_profiles.json`、编码不对的 `affection.json`，以及类型不对的 `preference_state.json`（列表）、`seen_videos.json`（列表）、`watch_log.json`（字典）。
