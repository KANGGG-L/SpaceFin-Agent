# ADR-0001：移除 Kafka/Flink 实时层，贷后预警改为 T+1 批处理

## 状态
已采纳（实时层代码已从仓库暂存删除）

## 迭代
第 1 轮迭代

## 背景（Context）
- 阶段 2 技术评估曾把 Kafka + Flink CEP 列为事中反欺诈的候选链路（`docs/product/02/README.md`）。
- 交付收敛到精简版（lite）后，实时层被认为超出当前交付周期：T+1 批处理已满足「AVM → LTV → 1104 → 驾驶舱」核心闭环的时效需求。
- 实时链路投入量级更高（Flink 状态后端、Kafka 运维、CEP 规则调优），与「核心闭环可复现、可归因」的取舍原则冲突。
- **观察数据**：核心闭环「AVM → LTV → 1104 → 驾驶舱」的时效需求为 T+1；实测 CDC 增量同步至 ODS 时延 **0.32s**（`cdc_ods_latency_seconds`）。实时层（Kafka+Flink CEP）未跑通，无时延/吞吐实测记录。

## 决策（Decision）
- 移除实时层：暂存删除 `tools/stream/*`、`deploy/kafka-flink/*`、`deploy/systemd/spacefin-stream-producer.service`、`docs/tech/components/kafka-flink-realtime.md`。
- 贷后预警改为 **T+1 批处理推送**，由 Airflow 排程（`ads_alert_dispatch` + site_inbox + 可选 webhook）。
- PRD 的 Non-goals 维持不变：实时反欺诈**不闭环**，仅做预警联动。

## 备选方案（Alternatives）
- 保留 Kafka/Flink CEP 实时链路：否决——当前交付周期内 T+1 已够用，运维成本高于收益。
- 用 Spark Streaming 微批次替代：否决——微批次时延（数百 ms~数秒）对「同设备 1 小时内多注册」这类时间窗判定不够精确。
- 保留代码但不排程：否决——留下无维护的死链路，与「不留不可解释物」原则冲突。

## 影响（Consequences）
**正面**
- 核心闭环不依赖流式组件，单机可复现；测试面随实时层一并收窄。
- 复杂度与运维负担下降。
- **改变后数据**：实时层代码整体删除（见「证据」）；预警时效由秒级事中降为 T+1。若需重新评估实时层，须先埋点事中拦截命中率与时延。

**代价**
- 失去秒级事中拦截能力，预警降级为 T+1，语义为「不闭环」。
- 阶段 2 的 L1 论证成为历史候选论证，需在文档中显式标注为快照，否则易误导读者。

## 证据（Evidence）
- `git status --short`（暂存删除）：`D  tools/stream/__init__.py`、`producer.py`、`rebuild.sh`、`sql/ltv_realtime.sql`、`tests/*`；`D  deploy/kafka-flink/docker-compose.yml`、`download_jars.sh`；`D  deploy/systemd/spacefin-stream-producer.service`；`D  docs/tech/components/kafka-flink-realtime.md`。
- `README.md` :: `已移除实时层`（精简版范围说明：已移除实时层，预警改 T+1 批处理推送）。
- `README.md` :: `在当前交付周期内，T+1 批处理已满足核心闭环的时效需求`（Kafka / Flink 实时层不在交付范围）；`README.md` :: `已移除（精简版：实时预警改为 T+1 批处理推送`（组件表 Kafka+Flink 实时 ❌ 已移除）；`README.md` :: `实时层 Kafka/Flink 已于精简版移除`（Flink 无 checkpoint，实时层已移除）。
- `docs/product/02/README.md` :: `实时层 Kafka/Flink 已移除`（历史决策快照说明：实时层 Kafka/Flink 已移除）。
- `docs/product/06/README.md` :: `精简版已移除 Kafka/Flink 实时层`（实时层说明：测试随之移除，预警改 T+1 批处理推送）；`docs/product/06/README.md` :: `PRD 明确 Non-goals（L1），仅做预警联动不闭环`（G7 实时反欺诈闭环为 Non-goals）。

## 取证来源（Provenance）
`documented`——删除动作可从 git 暂存区直接核对，现状与口径在 README / 阶段文档中有明确记录。

## 回退（Rollback）
- 恢复被删除的实时层代码：`tools/stream/`（producer / rebuild.sh / sql/ltv_realtime.sql / tests）与 `deploy/kafka-flink/`（docker-compose / download_jars.sh）需从历史版本取回，再接入系统单元 `deploy/systemd/spacefin-stream-producer.service`。
- 当前工作树中实时层代码不存在，无法就地回退；若无可用历史版本，等价替代是维持现有 **T+1 批处理**（当前行为）并显式标注降级。

## 关联（Related）
- [ADR-0007-evidence-grading-and-artifact-exclusion.md](ADR-0007-evidence-grading-and-artifact-exclusion.md)
- [docs/product/02/README.md](../product/02/README.md)
- [docs/product/03/README.md](../product/03/README.md)
- [docs/product/06/README.md](../product/06/README.md)
