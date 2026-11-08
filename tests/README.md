# 测试

测试不需要装 AstrBot：`tests/conftest.py` 和各测试文件会放入假的 `astrbot` 模块。

## 准备

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
```

`yt-dlp` 目前没有测试用到，网络慢的话可以先不装。`openai` 要装（向量检索的快照测试用到）；`pydantic` 不在 requirements.txt 里（平时由 AstrBot 带），跑测试要另装：`.venv/bin/pip install pydantic`。

## 运行

在仓库根目录：

```bash
.venv/bin/python -m pytest -q
```

2026-10-27 基线（main `1d12480`）：410 个通过，103 个子测试通过。

## 行为快照

`tests/test_proactive_flow_snapshot.py` 用假的 B站、模型和存储跑完整的主动浏览流程，逐条比对 `tests/snapshots/proactive_flows.json`。重构时快照必须不变；有意改变行为时才重新生成，并在 PR 说明里写原因：

```bash
BILIBOT_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_proactive_flow_snapshot.py
```

## 耦合统计

```bash
python3 scripts/coupling_report.py          # 表格
python3 scripts/coupling_report.py --json   # 只看总数
```

重构的每个 PR 把输出贴进说明，总数只降不升。
