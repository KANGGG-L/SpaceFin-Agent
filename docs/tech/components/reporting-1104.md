# 组件技术说明 · 1104 G11 资产质量报送（S4 L5 合规）

> **状态**：✅ 已实施（2026-08-05 落地）
> **能力地图层级**：L5 合规 — 1104 监管报送 + 口径一致性校验
> **所属系统**：tools/reporting（数据源 spacefin_crawler.ads_risk_class）

---

## 1. 它解决什么问题

风险引擎每天把 5000 笔贷款的五级分类汇总写进 `ads_risk_class`，但「能算出数」不等于「能送监管」。
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

| 环节     | 载体                            | 职责                                                 |
| -------- | ------------------------------- | ---------------------------------------------------- |
| 生成汇总 | `tools/risk/store.py`           | 从 `dws_risk_class` 现状 SQL 聚合写 `ads_risk_class` |
| 报送 CLI | `tools/reporting/main.py`       | 模板化 + 三出口校验 + 幂等 upsert + 导出             |
| 兼容入口 | `tools/reporting/g11_report.py` | 转发到 main.py（pipeline 既有引用）                  |

## 3. 三出口口径一致性校验（TC-08）

三个出口（当日/现状快照，均按五级分类逐级比对）：

| 出口  | 数据源                                | 含义                       |
| ----- | ------------------------------------- | -------------------------- |
| 出口① | `ads_risk_class`（stat_date=当日）    | 内部累计（风险引擎产物）   |
| 出口② | `dws_risk_class`（现状快照 SQL 聚合） | 明细聚合（独立复算的裁判） |
| 出口③ | 1104 模板行（本模块由出口①生成）      | 报送出口                   |

校验规则（3 个比对轴 × 5 档逐类比对，逐类 7 项，合计 35 项，外加 2 条全局恒等式，共 37 条校验）：

- `loan_count`：严格相等；
- `balance_total`：差 ≤ 0.01（分位容差）；
- `balance_pct`：差 ≤ 0.0001（万分之一）。

### 3.1 校验规则清单（共 37 条，`validate_consistency` 逐一落 `check_name`）

`validate_consistency`（`tools/reporting/main.py`）对五档分类逐类生成 7 条 check（5 × 7 = 35），
另加 2 条全局恒等式，共 **37 条**；任一命中即写 `ads_report_alert`（level=block）阻断报送。
清单与代码一一对应，可按 `check_name` 直接对账：

| #     | check_name 模板（×5 档）           | 比对口径                                         |
| ----- | ---------------------------------- | ------------------------------------------------ |
| 01–05 | `1104_vs_internal:{档}:loan_count` | 1104 模板行 vs internal 汇总：笔数严格相等       |
| 06–10 | `1104_vs_internal:{档}:balance`    | 1104 模板行 vs internal 汇总：余额差 ≤ 0.01      |
| 11–15 | `1104_vs_dws:{档}:loan_count`      | 1104 模板行 vs dws 明细聚合：笔数严格相等        |
| 16–20 | `1104_vs_dws:{档}:balance`         | 1104 模板行 vs dws 明细聚合：余额差 ≤ 0.01       |
| 21–25 | `1104_vs_dws:{档}:balance_pct`     | 1104 模板占比 vs dws 按余额重算占比：差 ≤ 0.0001 |
| 26–30 | `dws_vs_internal:{档}:loan_count`  | dws 明细聚合 vs internal 汇总：笔数严格相等      |
| 31–35 | `dws_vs_internal:{档}:balance`     | dws 明细聚合 vs internal 汇总：余额差 ≤ 0.01     |
| 36    | `dws_vs_internal:总额:balance`     | 两路径余额总额一致（内部累计 vs 明细聚合）       |
| 37    | `g11:占比之和:balance_pct`         | 五级占比之和 = 1（G11 恒等式，余额为 0 时跳过）  |

> 另有两道读取侧守卫（不计入 37 条，但同为阻断语义）：`load_internal` / `load_dws_agg`
> 遇到非五级 `risk_class`（NULL、历史遗留、新增档位）一律抛错阻断——静默丢弃会让三出口
> 在「都不含该档」的前提下假一致，报送总额少算却不告警。

任一分类任一项不一致 → 记 mismatch，写 `ads_report_alert`（level=block），**阻断报送**：
exit code 非 0、不写 `ads_1104_g11`（只落 blocked 状态的 CSV/JSON 供排查）。

**为什么校验要拉 dws 明细聚合做独立裁判**：`ads_risk_class` 是引擎产物，若引擎或增量消费链
出 bug，汇总表会与明细表静默漂移；明细聚合是唯一能从原始明细复算的出口，用它拦得住
「汇总错了但没人发现」的场景。

## 4. 表结构

### ads_1104_g11（报送结果表，幂等）

| 列            | 类型          | 说明                                 |
| ------------- | ------------- | ------------------------------------ |
| stat_date     | DATE          | 报送业务日（Asia/Shanghai）          |
| risk_class    | VARCHAR(8)    | 五级 + 合计                          |
| loan_count    | INT           | 笔数                                 |
| balance_total | DECIMAL(16,2) | 余额合计                             |
| balance_pct   | DECIMAL(8,4)  | 占比，**小数**（0.6918 表示 69.18%） |
| is_total      | TINYINT       | 合计标记（1=合计行）                 |
| etl_ts        | TIMESTAMP     | 写入时间                             |

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

## 6. 输出样例（2026-08-19）

| risk_class | loan_count | balance_total        | balance_pct |
| ---------- | ---------- | -------------------- | ----------- |
| 正常       | 4118       | 1,741,568,219.39     | 78.89%      |
| 关注       | 803        | 358,639,038.41       | 16.25%      |
| 次级       | 50         | 48,617,434.82        | 2.20%       |
| 可疑       | 24         | 48,968,364.50        | 2.22%       |
| 损失       | 5          | 9,729,850.93         | 0.44%       |
| **合计**   | **5000**   | **2,207,522,908.05** | **100.00%** |

三出口校验结果：**一致，报送通过**（`consistent=true`，`ads_report_alert` 无记录）。

## 7. 验收记录（2026-08-19）

| 项                                                       | 结果                                                                                                                                     |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| G11 合计行                                               | 5000 笔 / 2,207,522,908.05（= 5000 全量贷款）                                                                                            |
| 三出口一致（ads_risk_class / dws_risk_class / 1104模板） | 一致，逐分类余额/笔数/占比零漂移                                                                                                         |
| 口径不一致阻断（AC-05 / TC-05，--simulate-mismatch）     | 阻断 exit=1，3 条告警写入 ads_report_alert（`1104_vs_dws:正常:balance`、`dws_vs_internal:正常:balance`、`dws_vs_internal:总额:balance`） |
| 幂等重跑                                                 | 同 (stat_date, risk_class) upsert 覆盖，行数稳定                                                                                         |

## 8. 已知局限

- **校验只锁「余额/笔数/占比」总账口径**：分类标签本身（如某笔归「次级」而非「关注」）
  是风险引擎的 LTV 分类口径，本模块不复议引擎分类是否合理。
- **占比存小数、展示乘 100**：`ads_1104_g11.balance_pct` 与 `ads_risk_class` 同口径存小数
  （0.6918），CSV 模板按 1104 习惯展示百分数（69.18）。两侧单位不同属有意为之，转换
  只在导出层发生。
- **不重复造汇总**：依赖 `ads_risk_class` 已由风险引擎写好；报送前若引擎未跑或日期缺失，
  出口①（internal，缺档补零）与出口②（`dws_risk_class` 现状聚合，不过滤日期、通常非空）
  在笔数/余额上必然对不上 → 由三出口校验阻断（exit=1 + block 告警），并无独立的
  「数据存在性校验」。仅当整库无数据（出口②亦全零）时视为一致放行（报零表、不告警），见
  `tests/test_g11_consistency.py::test_all_zero_batch_is_consistent`。
