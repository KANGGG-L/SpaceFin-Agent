# 演示回填工具（tools/dev）

本目录提供 7 天风险演进剧本（`docs/demo/script_7d.md`）的灌数与验收工具。

## backfill_7d.py —— 7 天回填

按剧本逐日调用**真实引擎**（`tools/risk/main.py --date X --write-db` +
`tools/alerting/main.py --date X`），并执行广州挂牌价扰动（可回滚）与 AVM 重训。

```bash
# 完整回填：reset(回滚扰动+重训基线+重灌5000笔) → D1..D7
tools/orchestrator/.venv/bin/python tools/dev/backfill_7d.py --all
# 单日重跑 / 一键回滚广州扰动 / 验收
tools/orchestrator/.venv/bin/python tools/dev/backfill_7d.py --day 4
tools/orchestrator/.venv/bin/python tools/dev/backfill_7d.py --rollback-gz
tools/orchestrator/.venv/bin/python tools/dev/backfill_7d.py --verify
```

每日动作与诚实性说明见脚本模块文档；金额口径见验收 C 组。

## verify.py —— 验收 SQL 检查

按 `docs/demo/acceptance.md` 的 A/C/D 组逐项打勾：7 日齐全、广州事件传导比值、
处置闭环、金额对账、边界与诚实性。输出 `[PASS]/[FAIL]` 清单。

## 运行时注意

- 回填期间请暂停 `tools/cdc/consumer.py` 与 `tools/cdc/main.py`（CDC 增量消费会用
  当日 business_date 并发写 ads 表，破坏回填数据确定性）。暂停幂等，可随时重启。
- 广州挂牌价扰动的原值记录在 `ads_demo_gz_perturb`，`--rollback-gz` 一键恢复。
