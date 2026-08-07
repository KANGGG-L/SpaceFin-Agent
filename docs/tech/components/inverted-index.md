# 组件技术说明 · Doris 倒排索引审计检索（I-04，L5 合规）

> **状态**：✅ 已接入（Doris 中文倒排索引，毫秒级敏感词检索）
> **能力地图层级**：L5 合规
> **接口契约**：I-04（研发评审.md）——方向：内部；协议：SQL（`USING INVERTED, chinese`）；频率：审计查询时；失败处理：毫秒级、无独立集群依赖。

---

## 1. 为何引入

阶段 2 L5 合规选型（docs/product/02/README.md）明确：合规审计的瓶颈不在搜索能力，而在**搜索结果与风控上下文的关联**。把倒排索引内置于数仓热层（Doris ADS），避免"审计集群与风控数据脱节"，并满足 R6「合规检索需毫秒级定位」与 LR-3「合规零事故破防」护栏指标。

终选方案 B（倒排索引 + 内嵌可解释），而非方案 C（独立 ES 集群）——零额外组件、与风控数据同源、可审计。

## 2. 实现

### 2.1 合规审计表

在 Doris `ads` 库新建 `ads_compliance_audit`（字段：`id / audit_type / audit_text / biz_id / operator / created_at`），`audit_text` 为检索目标列。DDL 见 [`sql/doris/01_compliance_audit_inverted.sql`](../../sql/doris/01_compliance_audit_inverted.sql)，含 6 行合成样例行（含"包装流水""过桥资金""虚假收入证明"等敏感词）。

### 2.2 中文倒排索引

```sql
ALTER TABLE ads.ads_compliance_audit
  ADD INDEX idx_audit_text(audit_text) USING INVERTED PROPERTIES("parser" = "chinese");
BUILD INDEX idx_audit_text ON ads.ads_compliance_audit;
```

> Doris 2.0+ 支持 `parser = "chinese"`（结巴分词），单字/词组混合召回，适合中文敏感词定位。

### 2.3 检索接口

Python 接口 [`tools/compliance/inverted_search.py`](../../tools/compliance/inverted_search.py)：

```python
from tools.compliance.inverted_search import search_audit
rows, elapsed_ms = search_audit("包装流水")   # WHERE audit_text MATCH '包装流水'
```

连接复用仓库统一约定（Doris FE MySQL 协议 9030，root 空密码，见 `tools/lake/config.py`），可被 `DORIS_*` 环境变量覆盖。

## 3. 真机验证结果（Doris 在线实测）

| 关键词 | 命中数 | 耗时 |
|--------|-------|------|
| `包装流水` | 2 | ~32 ms |
| `过桥资金` | 2 | ~21 ms |
| `虚假收入证明` | 2 | ~16 ms |
| `量子计算`（无关词） | 0 | ~12 ms |

测试 [`tools/compliance/test_inverted_search.py`](../../tools/compliance/test_inverted_search.py)（pytest）3 项全绿：表与索引存在、敏感词命中、无关词零误报，且检索耗时 < 1000 ms。结论：**毫秒级定位达成**，符合 I-04 契约。

## 4. 合规

- 样例行均为合成文本，不含真实个人金融信息（同仓库整体合规声明）。
- 审计检索与既有 PII 脱敏 / `ads_export_audit` 审计通道正交：检索用于"定位待审记录"，导出仍走既有受控通道（AC-06）。

## 5. 已知约束（诚实标注）

- 当前为演示级数据（6 行），生产需由贷后 / 申请流水持续写入 `ads_compliance_audit`。
- `BUILD INDEX` 为异步操作；增量写入的新行由 Doris 自动维护倒排索引，无需重建。
- Doris 单副本（`replication_num=1`），与既有工程简化一致（见 README 工程简化清单）。
