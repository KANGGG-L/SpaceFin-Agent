#!/usr/bin/env bash
# SpaceFin 实时链路（Kafka + Flink）一键重建脚本。
#
# 什么时候用：
#   实时链路上游（Kafka topic / Flink 作业 / producer 水位）出现"看似正常但实际不通"
#   的状态，例如：Flink 作业在 UI 上 RUNNING，但 Kafka source 一直 poll 不到新数据、
#   inbox 长时间无新行。此时先看 docs/tech/components/kafka-flink-realtime.md 的
#   「故障排查与重建」章节，再用本脚本做一次干净重建。
#
# 本脚本做什么（按序）：
#   1) 备份现场状态（docker ps / topic offset / producer 水位 / inbox 水位）
#   2) 停实时链：停 producer → 取消 Flink 作业 → compose down（只停 Kafka/Flink，
#      不动 MySQL / CDC / 离线 consumer / 前端）
#   3) 起 Kafka 并等 broker 就绪 → 干净重建 topic（删旧建新，避免旧生命周期数据残留）
#   4) 重置 producer 水位到 ods_cdc_log 当前最大值（只转发重建后的新事件，不重放历史）
#   5) 起 Flink（JM + TM）并等注册就绪 → 重启 producer 常驻
#   6) 重提 Flink 作业并等 RUNNING
#   7) 端到端自检：改一笔贷款余额，断言 30s 内 inbox 出现新预警，报告耗时
#
# 约束：
#   - 只影响实时链（spacefin-stream-producer / kafka / flink），离线链（binlog CDC、
#     ods→dws 重算、前端驾驶舱）不受影响。
#   - 不改任何数据源表结构；自检会用 UPDATE 改一笔测试贷款余额（可配环境变量覆盖）。
#   - Flink 作业无 checkpoint/无保存点：重建后从 latest-offset 重新消费，历史积压不会
#     重放（这正是"仅推送不闭环、无状态作业"的设计语义，见 docs 第 7 节）。
#   - 密码不硬编码：MySQL root 密码从仓库 .env 读取；Kafka/Flink 交互全走 docker exec。
#
# 用法：
#   bash tools/stream/rebuild.sh
#   TEST_LOAN_ID=30002 bash tools/stream/rebuild.sh   # 自检换一笔贷款
#   TEST_BALANCE=600000.00 bash tools/stream/rebuild.sh  # 显式指定自检余额
#
# 退出码：0=链路已恢复且端到端自检通过；1=任一步骤失败（可重跑，脚本是幂等的）。

set -euo pipefail

# ---- 常量与路径 ----
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV_PY="$REPO_ROOT/tools/orchestrator/.venv/bin/python"
COMPOSE="docker compose -f $REPO_ROOT/deploy/kafka-flink/docker-compose.yml"
TOPIC="spacefin.cdc.log"
JOB_NAME="spacefin-ltv-realtime"
SQL_PATH="/opt/flink/sql/ltv_realtime.sql"          # 容器内挂载路径（compose 已 bind 好）
PRODUCER_UNIT="spacefin-stream-producer"
CRAWL_DB="spacefin_crawler"
BIZ_DB="spacefin"
TEST_LOAN_ID="${TEST_LOAN_ID:-30001}"
# 自检余额：默认留空，由第 9 步按该贷款估值动态算出（保证 LTV>0.85 且必改变）；
# 也可显式覆盖（如 TEST_BALANCE=600000.00，须保证确实改变且越线）。
TEST_BALANCE="${TEST_BALANCE:-}"

# 从 .env 读 MySQL root 密码（与 tools/risk/config.py 的解析口径一致，不硬编码）。
mysql_root_pw() {
  "$VENV_PY" - <<'PYEOF'
import os
p = os.path.join(os.environ["REPO_ROOT"], ".env")
env = {}
with open(p, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip("\"'")
print(env.get("MYSQL_ROOT_PASSWORD", ""))
PYEOF
}
ROOT_PW="$(REPO_ROOT="$REPO_ROOT" mysql_root_pw)"
[ -n "$ROOT_PW" ] || { echo "[rebuild] .env 里没有 MYSQL_ROOT_PASSWORD，退出"; exit 1; }

MYSQL="docker exec spacefin-mysql mysql -uroot -p$ROOT_PW"
: "${MYSQL}"  # 仅确保引用一次，避免 shellcheck 未使用告警

log() { echo "[rebuild] $(date +%H:%M:%S) $*"; }
die() { log "FAIL: $*"; exit 1; }

# 轮询直到某命令成功（最多 $2 秒，间隔 2s）。$1 为命令字符串。
wait_ok() {
  local cmd="$1" tries=$(( ${2:-60} / 2 ))
  for _ in $(seq 1 "$tries"); do
    if bash -c "$cmd" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

# ---- 1. 现场状态备份（改容器前先留证据） ----
BK_DIR="/tmp/spacefin-realtime-rebuild-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK_DIR"
log "备份现场状态 -> $BK_DIR"
docker ps > "$BK_DIR/docker-ps.txt" 2>&1 || true
docker exec spacefin-kafka /opt/kafka/bin/kafka-get-offsets.sh \
  --bootstrap-server localhost:9092 --topic "$TOPIC" > "$BK_DIR/topic-offsets.txt" 2>&1 || true
docker exec spacefin-mysql mysql -uroot -p"$ROOT_PW" -N -e \
  "SELECT 'producer_watermark', consumer, last_id FROM $CRAWL_DB.ods_cdc_consumer_offset WHERE consumer='kafka_stream_producer'; \
   SELECT 'inbox_max', COALESCE(MAX(event_id),0) FROM $CRAWL_DB.ads_stream_ltv_alerts;" \
  > "$BK_DIR/db-state.txt" 2>/dev/null || true

# ---- 2. 停实时链（只停实时链） ----
log "停 producer（systemd user 服务）"
systemctl --user stop "$PRODUCER_UNIT" || true

log "取消在跑 Flink 作业（先 REST 优雅 cancel，compose down 只是兜底）"
JIDS="$(curl -s http://localhost:8081/jobs/overview 2>/dev/null \
  | "$VENV_PY" -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print(" ".join(j["jid"] for j in d.get("jobs",[]) if j["state"] in ("RUNNING","RESTARTING")))
except Exception:
    print("")' || true)"
for jid in $JIDS; do
  curl -s -X PATCH "http://localhost:8081/jobs/$jid?mode=cancel" >/dev/null 2>&1 || true
  log "已请求取消作业 $jid"
done

log "compose down 停 Kafka + Flink"
(cd "$REPO_ROOT/deploy/kafka-flink" && docker compose down) || die "compose down 失败"
log "实时链已全停（离线链 MySQL/CDC/consumer/前端不受影响）"

# ---- 3. 起 Kafka 并等就绪 ----
log "起 Kafka"
(cd "$REPO_ROOT/deploy/kafka-flink" && docker compose up -d kafka) || die "起 Kafka 失败"
wait_ok "docker exec spacefin-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list" 120 \
  || die "Kafka broker 120s 未就绪"
log "Kafka 就绪"

# ---- 4. 干净重建 topic ----
# 为什么删旧建新而不是留着：Kafka 容器重建过、Flink 无 checkpoint 无已提交 offset，
# topic 里是上一次生命周期的消息；与其让新作业从 latest 跳到新端点（端点语义不清），
# 不如把 topic 清空让"重建后只收新事件"的语义完全干净。下游以 event_id 主键幂等，
# 清空不会产生脏数据。
log "重建 topic $TOPIC（删旧建新）"
docker exec spacefin-kafka /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 --delete --topic "$TOPIC" >/dev/null 2>&1 || true
sleep 3
docker exec spacefin-kafka /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 --create --topic "$TOPIC" --partitions 1 --replication-factor 1 >/dev/null \
  || die "创建 topic 失败"

# ---- 5. 重置 producer 水位 ----
# 水位置为 ods_cdc_log 当前最大 id：producer 只转发重建后的新事件。
# 注意不要置 0——那会让 producer 把几千条历史积压重新推到刚清空的 topic，纯属浪费，
# 且新作业 latest-offset 也只会从空 topic 的新端点开始。
log "重置 producer 水位到 ods_cdc_log 当前最大值"
docker exec spacefin-mysql mysql -uroot -p"$ROOT_PW" -e \
  "INSERT INTO $CRAWL_DB.ods_cdc_consumer_offset (consumer, last_id) VALUES ('kafka_stream_producer', (SELECT COALESCE(MAX(id),0) FROM $CRAWL_DB.ods_cdc_log)) ON DUPLICATE KEY UPDATE last_id=(SELECT COALESCE(MAX(id),0) FROM $CRAWL_DB.ods_cdc_log);" \
  || die "重置水位失败"
docker exec spacefin-mysql mysql -uroot -p"$ROOT_PW" -N -e \
  "SELECT consumer, last_id FROM $CRAWL_DB.ods_cdc_consumer_offset WHERE consumer='kafka_stream_producer';"

# ---- 6. 起 Flink 并等就绪 ----
log "起 Flink（JM + TM）"
(cd "$REPO_ROOT/deploy/kafka-flink" && docker compose up -d flink-jobmanager flink-taskmanager) \
  || die "起 Flink 失败"
wait_ok "curl -s http://localhost:8081/overview | grep -q '\"taskmanagers\":[1-9]'" 120 \
  || die "Flink JM/TM 120s 未就绪"
log "Flink 就绪"

# ---- 7. 重启 producer 常驻 ----
log "重启 producer（常驻轮询）"
systemctl --user start "$PRODUCER_UNIT" || die "producer 启动失败"
sleep 3
systemctl --user is-active "$PRODUCER_UNIT" >/dev/null || die "producer 未在运行"

# ---- 8. 重提 Flink 作业并等 RUNNING ----
log "重提 Flink 作业 $JOB_NAME"
docker exec -d flink-jobmanager /opt/flink/bin/sql-client.sh -f "$SQL_PATH" || die "作业提交失败"
wait_ok "curl -s http://localhost:8081/jobs/overview | grep -q '$JOB_NAME' && curl -s http://localhost:8081/jobs/overview | grep -q '\"state\":\"RUNNING\"'" 60 \
  || die "作业 60s 未 RUNNING"
log "作业 RUNNING"

# ---- 9. 端到端自检 ----
# 动态算一个"必触发且必改变"的余额：取该贷款的估值 V，设余额 = 0.95*V（LTV≈0.95>0.85
# 必越线），并保证与当前余额不同。教训：写死某个余额可能恰好等于现值，UPDATE 变成
# no-op，MySQL 行格式 binlog 不产生事件，自检会误报失败。
log "端到端自检：贷款 $TEST_LOAN_ID，动态计算测试余额并等待 inbox 新行"
VAL="$($MYSQL -N -e "SELECT market_valuation FROM $CRAWL_DB.dws_risk_class WHERE loan_id=$TEST_LOAN_ID LIMIT 1;" 2>/dev/null)"
[ -n "$VAL" ] || die "自检：贷款 $TEST_LOAN_ID 在 dws_risk_class 无估值，请换 TEST_LOAN_ID"
CUR_BAL="$($MYSQL -N -e "SELECT balance FROM $BIZ_DB.loan WHERE loan_id=$TEST_LOAN_ID;" 2>/dev/null)"
if [ -z "$TEST_BALANCE" ]; then
  TEST_BALANCE="$("$VENV_PY" -c 'import sys; print(f"{float(sys.argv[1])*0.95:.2f}")' "$VAL")"
  if [ "$TEST_BALANCE" = "$CUR_BAL" ]; then
    TEST_BALANCE="$("$VENV_PY" -c 'import sys; print(f"{float(sys.argv[1])*0.90:.2f}")' "$VAL")"
  fi
fi
log "估值=$VAL 当前余额=$CUR_BAL -> 测试余额=$TEST_BALANCE (LTV≈0.95)"
BASE="$($MYSQL -N -e "SELECT COALESCE(MAX(event_id),0) FROM $CRAWL_DB.ads_stream_ltv_alerts;" 2>/dev/null)"
T0=$(date +%s)
$MYSQL -e "UPDATE $BIZ_DB.loan SET balance=$TEST_BALANCE WHERE loan_id=$TEST_LOAN_ID;" \
  || die "自检 UPDATE 失败"
NEW_ROW=""
for _ in $(seq 1 15); do
  sleep 2
  NEW_ROW="$($MYSQL -N -e \
    "SELECT event_id, loan_id, loan_balance, ltv, risk_class FROM $CRAWL_DB.ads_stream_ltv_alerts WHERE event_id > $BASE ORDER BY event_id DESC LIMIT 1;" 2>/dev/null || true)"
  [ -n "$NEW_ROW" ] && break
done
T1=$(date +%s)
if [ -n "$NEW_ROW" ]; then
  log "自检通过：inbox 新行 event_id>$BASE -> $NEW_ROW（耗时 $((T1-T0))s）"
else
  log "FAIL：30s 内 inbox 无新行（基线 event_id=$BASE）。"
  log "      排查线索：TM 日志 /opt/flink/log/、producer journalctl --user -u $PRODUCER_UNIT、"
  log "      Kafka 日志 docker logs spacefin-kafka。自检失败但不回滚（现场保留供诊断）。"
  exit 1
fi

# ---- 10. 汇总 ----
log "完成：实时链已重建并自检通过。备份目录 $BK_DIR"
log "参考：docs/tech/components/kafka-flink-realtime.md「故障排查与重建」"
