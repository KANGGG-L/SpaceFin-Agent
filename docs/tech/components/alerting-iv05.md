# 组件技术说明 · LTV 预警推送（I-05，贷后保全）

> **状态**：✅ 已实施（2026-08-05 落地）
> **能力地图层级**：L4 应用接口 — 预警事件对外推送
> **所属系统**：tools/alerting（数据源 spacefin_crawler.ads_ltv_alerts）

---

## 1. 它解决什么问题

风险引擎每天把 LTV 超红线（0.85）的贷款写进 `ads_ltv_alerts`，但预警只有「落库」没有
「送达」：贷后保全系统必须在 T+1 内收到预警（TC-03），且不能因为批处理重跑、宕机补跑
把同一条预警推两遍。

本组件做推送执行器：
1. **清单生成**：`--date` 取当日预警（`ads_ltv_alerts WHERE alert_date = --date`）；
2. **T+1 去重**：同一 `(loan_id, alert_date)` 已推送成功的不重推；
3. **失败重试状态机**：单条推送失败自动重试，超限留终态待人工；
4. **driver 可替换**：当前推站内告警表 + 文件，真实贷后系统接口是预留替换点。

## 2. 推送语义与状态机

```
                    ┌─ 成功 ──▶ success（记 dispatch_ts）
pending ──▶ 尝试 ──┤
                    └─ 失败 ──▶ failed, attempt_count+1
                                ├─ attempt_count < max_retries ──▶ 下一轮自动重试
                                └─ attempt_count >= max_retries ──▶ 终态 failed（待人工）
```

- 状态存 `ads_alert_dispatch`，以 `UNIQUE(loan_id, alert_date)` 为去重键；
- 已 `success` 的记录直接跳过（去重）；`failed` 且未超限的自动重试；
- 重试上限 `--max-retries`（默认 3），每轮每行最多处理一次，杜绝同轮死循环。

**T+1 口径**：预警 T 日生成、最迟 T+1 送达贷后系统。生产调度把本 CLI 排在风险引擎之后
执行即可满足；配合去重，任何一天的补跑/重跑都不会重复推送。

## 3. Driver 替换点

驱动只负责「把一条预警送达」，不关心去重/重试——那是 main.py 的职责。见
`tools/alerting/drivers.py`：

| 驱动 | 去向 | 用途 |
|---|---|---|
| `site_inbox` | 站内告警表 `ads_alert_inbox`（UNIQUE(loan_id, alert_date) 幂等） | 默认推送通道 |
| `file` | `output/alerting/alert_push_<date>.jsonl`（JSONL 追加） | 本地联调 / 验收 |
| `postloan_http` | **预留**：真实贷后系统 HTTP 接口 | 替换点，接入后注册到 `make_driver` |

接入真实贷后系统：实现 `AlertDriver.send`（把预警组装成目标接口报文并调用，失败抛异常），
在 `make_driver` 注册，`--drivers postloan_http` 即可切换，调度层无需改动。

## 4. 表结构

### ads_alert_dispatch（推送台账）

| 列 | 类型 | 说明 |
|---|---|---|
| loan_id | INT | 贷款号 |
| alert_date | DATE | 预警生成日（T） |
| dispatch_date | DATE | 推送执行日 |
| status | VARCHAR(16) | pending/success/failed |
| attempt_count | INT | 已尝试次数 |
| max_retries | INT | 重试上限 |
| last_error | VARCHAR(255) | 最近一次失败原因 |
| dispatch_ts | DATETIME | 最近一次尝试时间（业务时区） |

UNIQUE `(loan_id, alert_date)` —— 去重的物理保证。

### ads_alert_inbox（站内告警表）

`id / loan_id / customer_id / collateral_id / loan_balance / market_valuation / ltv /
risk_class / is_high_risk_zone / alert_date / received_ts`，UNIQUE `(loan_id, alert_date)`
保证重试不产生重复站内消息。

## 5. 运行方式

```bash
# 推送当日预警（--date 默认 Asia/Shanghai 业务日；驱动缺省 site_inbox,file）
tools/orchestrator/.venv/bin/python tools/alerting/main.py --date 2026-08-05

# 只看清单不推送
tools/orchestrator/.venv/bin/python tools/alerting/main.py --date 2026-08-05 --dry-run

# 只推文件（本地联调）
tools/orchestrator/.venv/bin/python tools/alerting/main.py --date 2026-08-05 --drivers file

# 演练重试状态机（注入失败，看 failed → 重试 → 终态）
tools/orchestrator/.venv/bin/python tools/alerting/main.py --date 2026-08-05 --force-fail
```

输出：控制台推送摘要（候选/推送/去重/重试/失败/终态失败）+ `output/alerting/`
`alert_summary_<date>.json`（摘要）、`alert_dispatch_<date>.csv`（台账）、
`alert_push_<date>.jsonl`（文件驱动推送日志）。

## 6. 验收记录（2026-08-05，--date 2026-08-05）

| 场景 | 结果 |
|---|---|
| 首轮推送（16 条候选，含 site_inbox + file） | 16 条因文件驱动 Decimal 序列化失败 → `failed`（attempt=1） |
| 修复后重跑 | 16 条自动**重试**并全部推送成功（attempt=2） |
| 第三次重跑 | **去重 16**，0 条重推（`dedup=16`） |
| 站内告警表 | `ads_alert_inbox` 恰好 16 行（无重复） |
| 终态演练（--force-fail 两轮后） | 2 条进入 `final_failed`，后续轮次跳过 |

> 首轮失败是真实缺陷（FileDriver 未处理 Decimal → JSON 序列化），恰好验证了状态机的
> 失败→重试→成功路径；修复后数据与台账均符合预期。

## 7. 已知局限

- **driver 串行、整体成功判定**：同一预警要发给所有启用 driver，任一失败即整条记 failed
  并重试——当前通道都是幂等写，重试无害；若将来引入非幂等通道需按通道拆状态。
- **终态失败需人工介入**：超限的 `failed` 记录不会自动复活，需人工核对后清理/重推。
- **at-least-once 而非 exactly-once**：重试保证不丢，靠 `(loan_id, alert_date)` 唯一键
  保证不重复；跨库事务不在 MVP 范围。
