# 组件技术说明 · Kafka + Flink 实时接入（CDC → Kafka → Flink → 下游联动）

> **状态**：✅ 已实施（2026-08-05 落地，Workflow B）
> **能力地图层级**：L0 数据底座 — 变更数据流式化（补上 cdc-downstream 的"推"侧）
> **所属系统**：deploy/kafka-flink（部署）+ tools/stream（producer / Flink SQL 作业）

---

## 1. 它解决什么问题

[cdc-downstream.md](cdc-downstream.md) 里的链是**轮询式增量**：`spacefin-cdc-consumer`
每 20s 拉一次 `ods_cdc_log`，把变更"重算进 DWS/ADS"。它有一个硬伤：**下游是"拉"的，
没有事件总线**——多业务方要实时感知变更（预警推送、驾驶舱刷新、反欺诈风控）时，
各自去轮询一张日志表，水位、幂等、顺序都要重新发明。

本组件补上"推"侧：

```
业务库变更 ──spacefin-cdc──▶ ODS(ods_cdc_log) ──producer──▶ Kafka ──Flink SQL──▶ 实时预警 inbox
 (spacefin)      [binlog]     (spacefin_crawler)   (tools/stream)  (deploy/kafka-flink)  ads_stream_ltv_alerts
```

规划对齐：`docs/tech/开发计划.md` S2 里 L1 实时预警联动定义为**仅推送不闭环**（R-tech-1：
MVP 先 L0+L3，Flink 仅轻量联动）。本组件实现的就是这一条：事件到达 → 按 loan_id 实时算
LTV 越线 → 打一条预警消息进 inbox；**不做删除/状态机回写**，作业无状态、可随意重启。

## 2. 部署拓扑与端口

单机（4 核 / 15G）上的最小集群形态：

```
宿主  ┌──────────────────  docker compose（spacefin-realtime）──────────────────┐
      │  spacefin-kafka（apache/kafka:3.8.0，KRaft 单节点 broker+controller）     │
      │    端口：9092 EXTERNAL（宿主 producer 用）、9093 INTERNAL（Flink 用）、    │
      │           9094 CONTROLLER（仅容器内）                                     │
      │  flink-jobmanager（apache/flink:1.19.1）                                 │
      │    端口：8081 REST/UI、6123 JM RPC、6124/6125 blob/query（容器内）        │
      │  flink-taskmanager（apache/flink:1.19.1，1 slot，并行度 1）               │
      └────────────────────────────────────────────────────────────────────────┘
      共享外部网络 spacefin-agent_default —— 与 spacefin-mysql 同网，直接以服务名访问
```

**为什么用这两个镜像/形态**：

- **Kafka 用 KRaft 单节点**（不带 ZooKeeper）：4 核/15G 下少一个常驻进程；验收只要求
  「topic 可收发」，单副本足够。官方 `apache/kafka` 镜像自带存储格式化，无需手动
  `kafka-storage.sh`。
- **Flink 用 standalone（JM + 1 TM）**而非其它部署模式：官方镜像开箱即用、内存可精确
  上限，是最贴合本机资源（4 核 / 15G）的形态。作业用 **Flink SQL**（sql-client 提交，
  跑在 TM 上），不需要本机装 Java/打包 jar——连接器 jar 以 bind mount 挂进
  `/opt/flink/lib`。
- **全部容器设 `mem_limit` 防 OOM**：Kafka 1.5G（JVM `-Xmx1G`）、JM/TM 各 1.5G
  （进程内存 1024m，经 `FLINK_PROPERTIES` 覆盖镜像默认的 1600m/1728m）。

**端口防冲突**：9092/9093/8081/6123 均不与现有服务重叠（MySQL 3306、Redis 6379、
Airflow 8080；Doris/MinIO 的 8030/9030/8040/9000 归另一组件）。8082 未占用：单 JM
不需要第二个 UI 端口。

## 3. 连接器与配置

- 连接器 jar（`deploy/kafka-flink/download_jars.sh` 下载，Maven Central，与 Flink 1.19
  匹配）：`flink-sql-connector-kafka-3.3.0-1.19.jar`、`flink-connector-jdbc-3.2.0-1.19.jar`、
  `mysql-connector-j-8.0.33.jar`。json 格式解析 `flink-json` 发行版自带。
- Flink 内存/地址覆盖走官方镜像的 `FLINK_PROPERTIES` 环境变量（entrypoint 会以 `-D`
  写进 `conf/config.yaml`）；`JOB_MANAGER_RPC_ADDRESS=flink-jobmanager` 让 TM 与
  sql-client 都用容器名找到 JM。

## 4. topic 约定

| topic | 写入方 | 消费方 | 说明 |
|---|---|---|---|
| `spacefin.cdc.log` | tools/stream/producer.py | Flink 作业 | 全量 ODS 变更（loan/collateral/customer），消息含 `table_name`，作业自行过滤 |

消息 JSON（loan 平铺 + payload 全量）：

```json
{
  "event_id": 2210, "table_name": "loan", "event_type": "UPDATE",
  "cdc_ts": "2026-08-05 00:00:00", "biz_date": "2026-08-05",
  "loan_id": 30003, "customer_id": 10003, "collateral_id": 20003,
  "balance": 500000.00, "interest_rate": 5.98, "risk_class": "关注",
  "payload": { "loan_id": 30003, "balance": 500000.0, "...": "after_json 全量" }
}
```

- `event_id` 即 `ods_cdc_log.id`，是天然幂等键。
- `biz_date` 按 Asia/Shanghai 生成（`tools/risk/config.py: business_date`），预警日口径
  与离线链路一致，避开宿主 UTC / MySQL +08 的时区坑。

## 5. 实时链路（producer + Flink SQL）

### 5.1 producer（tools/stream/producer.py）

- 读 `ods_cdc_log` 增量（水位复用 `ods_cdc_consumer_offset`，consumer 名
  `kafka_stream_producer`，与离线 `risk_downstream` 互不干扰），以 `id` 升序拉取 +
  单向推进水位 = **至少一次**语义；下游按 `event_id` 主键幂等。
- 只做**贴源转发**（语义留给下游解释），与 `tools/cdc/consumer.py` 的解耦思路一致。
- 运行：
  ```bash
  # 常驻轮询（每 2s）——生产用法
  tools/orchestrator/.venv/bin/python tools/stream/producer.py --loop --interval 2
  # 单批转发后退出（验收/对账用）
  tools/orchestrator/.venv/bin/python tools/stream/producer.py --once
  tools/orchestrator/.venv/bin/python tools/stream/producer.py --once --from-offset 0
  ```
  依赖 `kafka-python`（已装入 tools/orchestrator/.venv）。

### 5.2 Flink SQL 作业（tools/stream/sql/ltv_realtime.sql）

```
Kafka(spacefin.cdc.log) ──过滤 loan 事件──▶ 与 dws_risk_class 查找 join（PROCTIME）
   ──LTV = 事件新余额 / 离线 AVM 估值──▶ 越线(>0.85) ──▶ INSERT ads_stream_ltv_alerts
```

- **联动语义**：余额一变，立刻用"离线批维护的 AVM 估值"算新 LTV 是否越线。维度表用
  JDBC 查找 join（10s 缓存），估值是 `dws_risk_class.market_valuation`。
- 五级分类阈值与 `tools/risk/config.py` 的 `CLASS_LTV_UPPER` 保持一致（0.60/0.75/0.85/1.00）。
- 提交：
  ```bash
  docker exec -d flink-jobmanager /opt/flink/bin/sql-client.sh -f /opt/flink/sql/ltv_realtime.sql
  ```
  作业名 `spacefin-ltv-realtime`，可在 `http://localhost:8081` 看到 RUNNING 与吞吐。

### 5.3 落点（tools/stream/init_db.py）

`ads_stream_ltv_alerts`（`spacefin_crawler` 库，`event_id` 主键，`alert_date` 业务日）：
实时告警 inbox，与离线 `ads_ltv_alerts`（按 贷款×日 替换）分开，避免互相覆盖。

## 6. 验证结果（2026-08-05）

| 项 | 结果 |
|---|---|
| Kafka broker 就绪 | ✅ topic `spacefin.cdc.log` 创建/列出成功（offset 水位随消息推进） |
| Flink 集群可用 | ✅ UI `http://localhost:8081` 正常，1 TM/1 slot 注册，作业 RUNNING |
| 改一笔 loan → 下游可见（实时链路） | **3.0s**（binlog→ODS 1.0s，Kafka+Flink→inbox 2.0s） |
| 是否只处理增量 | ✅ 重启后仅新事件（inbox 行数 65→66，delta=1，无历史重放） |
| 作业异常 | 0（job exceptions / TM 日志无 error，仅 kafka shaded jar 的无害 WARN） |
| 内存占用（容器内） | Kafka 0.32G / JM 0.46G / TM 0.47G，均 < 1.5G limit；宿主可用 9.7G |

实测样例（三次越线测试，均有 inbox 行）：

| 事件 | 贷款 | 新余额 | 估值 | LTV | 分类 |
|---|---|---|---|---|---|
| 2208 | 30001 | 1,500,000.00 | 1,556,396.54 | 0.9638 | 可疑 |
| 2209 | 30002 | 550,000.00 | 486,286.58 | 1.1310 | 损失 |
| 2210 | 30003 | 500,000.00 | 548,659.75 | 0.9113 | 可疑 |

端到端证据链：`UPDATE loan` → `ods_cdc_log` 出现事件 → producer 水位推进并写入
`spacefin.cdc.log` → Flink 作业消费（recordsConsumed 持续增长）→ `ads_stream_ltv_alerts`
多出 `event_id=2210` 且字段与 SQL 计算完全一致。

## 7. 已知局限（如实记录）

- **水位是"至少一次"而非 exactly-once**：producer 以 MySQL offset 表推进、Flink 消费
  无 checkpoint（无状态作业，未开 checkpoint 以省内存）。重复事件靠 `event_id` 主键
  幂等消化，与离线链路的取舍一致。若后续要做端到端恰好一次，需给 Flink 开 checkpoint
  并让 JDBC sink 支持 upsert。
- **估值维度有 10s 缓存**：余额变更后若估值也刚变，最多滞后 10s 才看到新估值。实时链
  路看的是"余额漂移"，估值刷新仍由离线批负责（这正是"仅联动不闭环"的边界）。
- **首次启动会消费 Topic 内积压**：`scan.startup.mode=latest-offset` 对空 Topic 起
  步时水位在 0，若 producer 已在转发历史，作业会把积压也消费掉（本机实测消费了
  2207 条历史并产出 63 条越线 inbox）。**正确启动顺序**：先起 producer 转发完积压，
  再起 Flink 作业；或重起作业（无 checkpoint → 无已提交 offset，latest 从当前端开始）。
  文档按重启后的干净状态验收。
- **凭据为 dev-only**：Flink SQL 文件里硬编码 `root/spacefin_dev_only`（仓库 .env 的
  开发值）。生产接入应换最小权限账号 + secret 管理，并给 `spacefin_crawler_app` 补
  `CREATE` 权限（当前建表走 root，日常读写 app 用户已够用）。
- **并发度 1 / 1 slot**：单 TM 单 slot、并行度 1。对"事件秒级联动"验收足够；若要扛
  高吞吐需扩 TM（4 核 / 15G 下最多再加 1 个 TM）。
- **producer 已做 systemd 托管**：用户级服务 `spacefin-stream-producer.service`
  （`systemctl --user`），`Restart=always` 保证常驻；宿主重启后 `loginctl enable-linger`
  已开启，服务会自动拉起。Flink/Kafka 由 compose 管，
  `docker compose -f deploy/kafka-flink/docker-compose.yml up -d` 拉起。

## 8. 运行命令速查

```bash
# 部署（首次需先下 jar）
bash deploy/kafka-flink/download_jars.sh
docker compose -f deploy/kafka-flink/docker-compose.yml up -d

# 建表 + 起 producer（常驻）
tools/orchestrator/.venv/bin/python tools/stream/init_db.py
nohup tools/orchestrator/.venv/bin/python tools/stream/producer.py --loop --interval 2 &

# 提交实时作业（Flink UI: http://localhost:8081）
docker exec -d flink-jobmanager /opt/flink/bin/sql-client.sh -f /opt/flink/sql/ltv_realtime.sql

# 实时链"看似正常但不通"时：一键重建（见第 9 章）
bash tools/stream/rebuild.sh

# 验收：改一笔贷款，看 inbox
mysql -h127.0.0.1 -uroot -pspacefin_dev_only spacefin_crawler \
  -e "UPDATE spacefin.loan SET balance=500000.00 WHERE loan_id=30003;"
mysql ... -e "SELECT event_id, loan_id, loan_balance, ltv, risk_class, alert_date
              FROM spacefin_crawler.ads_stream_ltv_alerts ORDER BY event_id DESC LIMIT 3;"
```

## 9. 故障排查与重建（2026-08-05 实战）

### 9.1 现象与根因

**现象**（Kafka 容器重建、加 volume 持久化后重提作业时出现）：
Flink 作业在 UI 上显示 **RUNNING**，但 Kafka source 不再消费新事件：
topic offset 有新事件（producer 正常发）而 inbox 无新行；TM 日志出现
`Assigned to partition(s)` + `Seeking to offset N` 之后长期无任何 poll/heartbeat 日志；
Kafka 日志出现 `SocketServer ... Unexpected error from /<Flink IP>` 连接被关闭。

**根因（证据链）**：

1. **静默停摆而不是崩溃**：作业一直 RUNNING，TM 日志无异常，只是 Kafka consumer
   不再发出任何请求，Flink 侧的 restart-strategy 永远等不到触发条件。
2. **静默连接被断开且不重建**：Flink Kafka source 在无事件时会长时间不发请求（实测
   从启动到断连期间连接上无任何 I/O）；而 Kafka client 与 broker 两侧的
   `connections.max.idle.ms` 默认都是 9 分钟，会把"静默"的连接断开。实测 consumer
   在 `Assigned to partition` 后**恰好 9 分钟整**打出一条 `Node -1 disconnected`；
   随后线程转储显示 fetcher 线程卡在 `KafkaConsumer.poll → Selector.poll(epoll)`，
   `/proc/net/tcp` 里与 kafka:9093 只剩 TIME_WAIT/半开 socket——**连接断了，但客户端
   没有重建连接**，新事件因此永远 poll 不到。
3. **触发场景**：Kafka 重建 / Flink 重启后的状态恢复期，topic 恰好处于 >9 分钟的
   静默期，最容易触发上述断连。consumer group `flink-ltv-realtime` 在 Kafka 侧不存在
   是 Flink KafkaSource 用 `consumer.assign()` 直接分配分区的**正常现象**，不能当故障信号。

### 9.2 修复（2026-08-05）

- 客户端侧：`tools/stream/sql/ltv_realtime.sql` 的 Kafka source 增加
  `'properties.connections.max.idle.ms' = '86400000'`（24h），客户端不再主动断开静默连接。
- broker 侧：`deploy/kafka-flink/docker-compose.yml` 增加
  `KAFKA_CONNECTIONS_MAX_IDLE_MS: "86400000"`，broker 不再主动断开静默连接。
- 两侧必须一起改：只改一侧，另一侧仍会在 9 分钟时断开；而该 kafka-clients 版本断连后
  重建连接不可靠（9.1），照样停摆。
- 实测：修复后重建链路，静默 >10 分钟连接保持（`est=2`），新事件照常约 2s 进 inbox。

### 9.3 一键重建：tools/stream/rebuild.sh

用于"实时链看似正常但不通"时做一次干净重建（幂等、可重跑、只动实时链）：

```bash
bash tools/stream/rebuild.sh          # 完整重建 + 端到端自检（自检余额动态算）
TEST_LOAN_ID=30002 bash tools/stream/rebuild.sh   # 自检换一笔贷款
TEST_BALANCE=600000.00 bash tools/stream/rebuild.sh  # 显式指定自检余额（须保证越线且改变）
```

流程：备份现场 → 停 producer → 取消 Flink 作业 → compose down → 起 Kafka →
**干净重建 topic**（删旧建新）→ **重置 producer 水位**到 `ods_cdc_log` 当前 `max(id)`
（只转发重建后的新事件，不重放历史）→ 起 Flink → 重启 producer → 重提作业 →
端到端自检（默认动态构造一个"必越线且必改变"的余额：取该贷款估值 V，设余额=0.95×V，
LTV≈0.95>0.85 必触发；若与当前余额相同再退到 0.90×V，保证 UPDATE 产生 binlog 事件），
断言 2s 内 inbox 出新行。

自检失败会保留现场（备份目录 `/tmp/spacefin-realtime-rebuild-*`）并以非 0 退出，
方便继续诊断。

### 9.4 第二层根因：topic 生命周期残留 + 作业无 checkpoint

9.1~9.2 的 idle-ms 修复解决的是"连接被静默断开且不重建"；但**Kafka 容器被重建过**
（compose down/up、改配置、宿主重启）之后，还会出现另一层问题：

- **topic 残留上一次生命周期的消息**：Kafka 容器重建后 topicId/epoch 变化，旧生命周期
  的积压消息仍躺在 topic 里（本机 `/srv/spacefin-lake/kafka/data` 挂载持久化，重建不会
  清空数据）。
- **作业无 checkpoint、无保存点**（本组件按"仅推送不闭环"设计，第 7 节）：重提作业时
  source 没有已提交 offset，`scan.startup.mode=latest-offset` 的"最新端点"语义在
  "含旧生命周期消息的 topic + 新生命周期 broker"下是不干净的——表现为作业 RUNNING、
  但 source 不 poll、consumer group 不注册，inbox 长时间无新行，与 9.1 现象一致。

**为什么不"留着 topic 直接重提"**：没有 checkpoint 就无从分辨新旧生命周期的消息边界；
与其让新作业从 latest 跳到语义不清的端点，不如把 topic 清空，让"重建后只收新事件"
的语义完全干净。下游 `ads_stream_ltv_alerts` 以 `event_id` 主键幂等，清空不产生脏数据
（详见 `tools/stream/rebuild.sh` 第 4 步注释）。

### 9.5 适用范围：只影响实时链

- rebuild.sh 只停/起实时链的进程与容器：`spacefin-stream-producer`、Kafka、Flink。
- **不触碰**：MySQL（业务库 + ODS）、`spacefin-cdc`（binlog→ODS）、
  `spacefin-cdc-consumer`（ODS→DWS/ADS）、前端驾驶舱（8500）、Airflow 采集。
- 重建期间离线链照常跑，DAG 的 `cdc_consume`/`risk_recalc` 不受影响；但实时 inbox
  会短暂停写（Kafka/Flink 停机到重提作业之间），预计 2~3 分钟。
- 自检用 `UPDATE` 改一笔贷款余额（默认 30001，`TEST_LOAN_ID` 可覆盖），会产生一条
  新的合法预警行；**不会自动恢复余额**，验收后请手动改回原值。

### 9.6 Flink 作业无保存点重启即丢的说明

- 本作业按设计是**无状态**的（仅推送不闭环，见第 7 节）：不开 checkpoint、不落保存点。
- 因此**任何重启**（rebuild.sh、compose down/up、cancel+重提）都会从
  `scan.startup.mode=latest-offset` 重新开始：重建时 topic 被清空、producer 水位被
  重置到当前 `max(id)`，**历史事件不会重放**，只有重建后的新事件会被处理。
- 如需重放某段历史：用 `producer.py --once --from-offset <id>` 把积压重新推一遍
  （下游按 `event_id` 主键幂等）；Flink 侧暂无保存点能力，不要指望重启后还能捡回
  断点前的数据。
