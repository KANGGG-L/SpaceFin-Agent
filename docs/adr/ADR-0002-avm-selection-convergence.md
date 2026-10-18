# ADR-0002：技术选型收敛为 HistGBR + cKDTree（MGWR+GBDT / Sedona / Hive+Doris 为历史候选）

## 状态
已采纳

## 迭代
第 2 轮迭代

## 背景（Context）
- 阶段 2 技术可行性评估中，多种候选方案被系统论证：湖仓一体（Hive+Doris）、AVM 的 MGWR+GBDT 混合、空间计算 Apache Sedona、实时 Kafka+Flink CEP。
- 交付收敛到精简版后，多个候选**未被实际引入**，而采用更轻的等价实现。
- 阶段 2 文档保留原论证以备追溯，并加了「⚠️ 历史决策快照说明」。
- **观察数据**：全局基线（面积中位数单价）全量 MAPE **20.58%**（`avm_baseline_r2`，r11 为 20.64%）；误差过大，需空间感知特征。

## 决策（Decision）
- AVM：`tools/avm/train.py` 使用 `HistGradientBoostingRegressor` 单模型 + 坐标最近邻（`sklearn.neighbors.NearestNeighbors`），**不引入 mgwr 库**。
- 空间：`tools/spatial/features.py` 使用 `scipy.spatial.cKDTree` 单机近似，**不引入 Sedona**；保留 `tools/spatial/sedona_demo.py` 作为架构等价性演示。
- 湖仓：Doris 热存 + MinIO 冷湖，**Hive 未实际引入**。

## 备选方案（Alternatives）
- MGWR + GBDT 混合架构：否决——需 mgwr 库与逐变量带宽校准；收敛后用 HistGBR + 坐标特征即可达成目标。
- Apache Sedona（PySpark 分布式）：否决——本机 DWD 有坐标行约 1.3 万，规模够不上分布式；cKDTree 等价近似足够。
- Hive + Doris 湖仓一体：否决——Hive 未实际引入，用 Doris + MinIO 实现分层。
- 实时 Kafka+Flink：见 [ADR-0001](ADR-0001-realtime-layer-removal.md)（已移除）。

## 影响（Consequences）
**正面**
- 技术栈显著收窄，零重依赖 / 单机可跑，可解释与可维护性提升。
- `sedona_demo.py` 保留，架构等价性论证未丢失。
- **改变后数据**：45% 覆盖 MAPE **9.94%**（`avm_mape_at_45pct_r2`，r11 为 9.88%）、全量 MAPE **15.00%**（`avm_mape_full_r2`）；oracle 下界 **12.65%**（`avm_error_floor_oracle`）。

**代价**
- 牺牲分布式空间计算与多尺度带宽估计的潜在上限；大规模场景需另建集群路径。
- 阶段 2 文档若不加以快照说明会误导读者（已加注缓解）。

## 证据（Evidence）
- `docs/product/02/README.md` :: `最终实现已按阶段 5/6 交付口径收敛`（历史决策快照：MGWR+GBDT / Sedona / Hive+Doris / Flink 为候选，最终收敛为 HistGBR / cKDTree / Doris+MinIO / 无实时层）。
- `docs/product/02/README.md` :: `MGWR + GBDT 混合架构`、`docs/product/02/README.md` :: `Apache Sedona（PySpark 分布式空间计算）`、`docs/product/02/README.md` :: `MGWR（多尺度地理加权回归）+ GBDT`（各候选原始论证与对比表）。
- `tools/avm/train.py` :: `from sklearn.ensemble import HistGradientBoostingRegressor`（模型导入）、`tools/avm/train.py` :: `model = HistGradientBoostingRegressor(`（模型实例化）、`tools/avm/train.py` :: `from sklearn.neighbors import NearestNeighbors`（坐标近邻）。
- `tools/spatial/features.py` :: `from scipy.spatial import cKDTree`、`tools/spatial/features.py` :: `DWD 有坐标的行仅约 1.3 万，规模远够不上分布式`（规模远够不上分布式）。
- `tools/spatial/sedona_demo.py` :: `Sedona 空间计算架构等价性演示`（Sedona 架构等价性演示）。
- `README.md` :: `Sedona → cKDTree 单机近似`；`README.md` :: `Hive → MinIO + S3 TVF`（Sedona → cKDTree 单机近似；Hive → MinIO + S3 TVF）。

## 取证来源（Provenance）
`documented`——最终实现可从代码直接核对，候选与收敛口径在阶段 2 文档中有明确记录。

## 回退（Rollback）
- 模型层：经 `tools/avm/train.py` 重训并切换模型版本（版本记录在 `output/avm/avm_report.json` 的 `version`），调用方无需改动。
- 空间层：近似实现由 `tools/spatial/features.py`（scipy cKDTree）承载；若要恢复 Sedona 分布式路径，先以 `tools/spatial/sedona_demo.py` 的架构等价性演示核实理由。

## 关联（Related）
- [ADR-0001-realtime-layer-removal.md](ADR-0001-realtime-layer-removal.md)
- [docs/product/02/README.md](../product/02/README.md)
- [docs/tech/components/spatial-feature.md](../tech/components/spatial-feature.md)
- [tools/avm/README.md](../../tools/avm/README.md)
