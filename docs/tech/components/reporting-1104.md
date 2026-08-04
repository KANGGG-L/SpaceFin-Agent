# 组件技术说明 · 1104 G11 资产质量报送（S4 L5 合规）

> **状态**：✅ 已实施（2026-08-05 落地）
> **能力地图层级**：L5 合规 — 1104 监管报送 + 口径一致性校验
> **所属系统**：tools/reporting（数据源 spacefin_crawler.ads_risk_class）

---

## 1. 它解决什么问题

风险引擎每天把 200 笔贷款的五级分类汇总写进 `ads_risk_class`，但「能算出数」不等于「能送监管」。
1104 G11 报表要求固定的五级模板（正常/关注/次级/可疑/损失 + 合计），且**口径必须可信**——
报送数字一旦与内部明细/台账对不上，轻则返工，重则监管处罚（R-UBQ-02）。

本组件把两条职责收口：
1. **模板化**：把 `ads_risk_class` 按 G11 模板重排，落 `ads_1104_g11` 表并导出 CSV/JSON；
2. **口径门禁**：三出口一致性校验，不一致则**阻断报送**并写 `ads_report_alert` 告警
   （宁可拒报，不可出错的报）。

## 2. 链路与分工

```
tools/risk/main.py --write-db ──▶ spacefin_crawler.ads_risk_class（五级汇总）
      ──▶ tools/reporting/main.py ──▶ ads_1104_g11（报送表）＋ ads_report_alert（口径告警）
      ──▶ output/reporting/ads_1104_g11_<date>.csv / g11_report_<date>.json
```

| 环节 | 载体 | 职责 |
|---|---|---|
| 生成汇总 | `tools/risk/store.py` | 从 `dws_risk_class` 现状 SQL 聚合写 `ads_risk_class` |
| 报送 CLI | `tools/reporting/main.py` | 模板化 + 三出口校验 + 幂等 upsert + 导出 |
| 兼容入口 | `tools/reporting/g11_report.py` | 转发到 main.py（pipeline 既有引用） |

## 3. 三出口口径一致性校验（TC-08）

三个出口（当日/现状快照，均按五级分类逐级比对）：

| 出口 | 数据源 | 含义 |
|---|---|---|
| 出口① | `ads_risk_class`（stat_date=当日） | 内部累计（风险引擎产物） |
| 出口② | `dws_risk_class`（现状快照 SQL 聚合） | 明细聚合（独立复算的裁判） |
| 出口③ | 1104 模板行（本模块由出口①生成） | 报送出口 |

校验规则（逐分类）：
- `loan_count`：严格相等；
- `balance_total`：差 ≤ 0.01（分位容差）；
- `balance_pct`：差 ≤ 0.0001（万分之一）。

任一分类任一项不一致 → 记 mismatch，写 `ads_report_alert`（level=block），**阻断报送**：
exit code 非 0、不写 `ads_1104_g11`（只落 blocked 状态的 CSV/JSON 供排查）。

**为什么校验要拉 dws 明细聚合做独立裁判**：`ads_risk_class` 是引擎产物，若引擎或增量消费链
出 bug，汇总表会与明细表静默漂移；明细聚合是唯一能从原始明细复算的出口，用它拦得住
「汇总错了但没人发现」的场景。

## 4. 表结构

### ads_1104_g11（报送结果表，幂等）

| 列 | 类型 | 说明 |
|---|---|---|
| stat_date | DATE | 报送业务日（Asia/Shanghai） |
| risk_class | VARCHAR(8) | 五级 + 合计 |
| loan_count | INT | 笔数 |
| balance_total | DECIMAL(16,2) | 余额合计 |
| balance_pct | DECIMAL(8,4) | 占比，**小数**（0.6918 表示 69.18%） |
| is_total | TINYINT | 合计标记（1=合计行） |
| etl_ts | TIMESTAMP | 写入时间 |

PK `(stat_date, risk_class)`；upsert 幂等（ON DUPLICATE KEY UPDATE）。

### ads_report_alert（口径告警表）

`id / report_date / report_type / alert_level / check_name / detail / etl_ts`。
校验不过时逐条写入，供合规角色核查。

## 5. 运行方式

```bash
# 报送当日 G11（--date 默认 Asia/Shanghai 业务日）
tools/orchestrator/.venv/bin/python tools/reporting/main.py --date 2026-08-05

# 演练阻断路径（人为让 dws 聚合偏移 1 元，验证 AC-05 门禁，不污染数据）
tools/orchestrator/.venv/bin/python tools/reporting/main.py --date 2026-08-05 --simulate-mismatch

# 只计算与校验，不写库
tools/orchestrator/.venv/bin/python tools/reporting/main.py --dry-run

# 指定输出目录
tools/orchestrator/.venv/bin/python tools/reporting/main.py --date 2026-08-05 --out-dir output/reporting
```

退出码：通过 = 0；阻断 = 1（pipeline 里 g11 步借此让整条链中断，见 run_pipeline.py 注释）。

## 6. 输出样例（2026-08-05）

| risk_class | loan_count | balance_total | balance_pct |
|---|---|---|---|
| 正常 | 127 | 39,521,397.34 | 48.29% |
| 关注 | 47 | 20,916,787.23 | 25.56% |
| 次级 | 10 | 7,005,210.35 | 8.56% |
| 可疑 | 10 | 7,777,807.57 | 9.50% |
| 损失 | 6 | 6,628,233.65 | 8.10% |
| **合计** | **200** | **81,849,436.14** | **100.00%** |

三出口校验结果：**一致，报送通过**（`consistent=true`，`ads_report_alert` 无记录）。

## 7. 验收记录（2026-08-05）

| 项 | 结果 |
|---|---|
| G11 合计行 | 200 笔 / 81,849,436.14（= 200 全量贷款） |
| 三出口一致（ads_risk_class / dws_risk_class / 1104模板） | 一致，逐分类余额/笔数/占比零漂移 |
| 口径不一致阻断（AC-05 / TC-05，--simulate-mismatch） | 阻断 exit=1，2 条告警写入 ads_report_alert |
| 幂等重跑 | 同 (stat_date, risk_class) upsert 覆盖，行数稳定 |

## 8. 已知局限

- **校验只锁「余额/笔数/占比」总账口径**：分类标签本身（如某笔归「次级」而非「关注」）
  是风险引擎的 LTV 分类口径，本模块不复议引擎分类是否合理。
- **占比存小数、展示乘 100**：`ads_1104_g11.balance_pct` 与 `ads_risk_class` 同口径存小数
  （0.6918），CSV 模板按 1104 习惯展示百分数（69.18）。两侧单位不同属有意为之，转换
  只在导出层发生。
- **不重复造汇总**：依赖 `ads_risk_class` 已由风险引擎写好；报送前若引擎未跑或日期缺失，
  数据存在性校验（出口①笔数=0）会直接阻断。
