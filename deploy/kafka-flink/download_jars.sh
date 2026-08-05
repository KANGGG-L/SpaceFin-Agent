#!/usr/bin/env bash
# 下载 Flink SQL 作业所需的连接器 jar 到 ./jars/（与 docker-compose 的 bind mount 对应）。
#
# 为什么单独下 jar 而不是打进镜像：
#   - 镜像复用官方 apache/flink:1.19.1，连接器按需挂载，版本可独立升级；
#   - 仓库不收录二进制，jars/ 由本脚本生成，避免大文件混进版本库。
# 版本选择：与 Flink 1.19 匹配的官方 release（Maven Central 已验证存在）。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/jars"
mkdir -p "$DIR"
BASE="https://repo.maven.apache.org/maven2/org/apache/flink"

download() {
  local url="$1"
  local out="$2"
  if [ -f "$out" ]; then
    echo "skip  $(basename "$out") (已存在)"
    return
  fi
  echo "fetch $(basename "$out")"
  curl -fsSL --retry 3 -o "$out" "$url"
}

# Kafka SQL 连接器（fat jar，含 kafka-clients；topic 的 source/sink 都靠它）。
download "$BASE/flink-sql-connector-kafka/3.3.0-1.19/flink-sql-connector-kafka-3.3.0-1.19.jar" \
  "$DIR/flink-sql-connector-kafka-3.3.0-1.19.jar"

# JDBC 连接器（thin jar，MySQL 驱动单独下；dim 查找 + MySQL sink 都需要）。
download "$BASE/flink-connector-jdbc/3.2.0-1.19/flink-connector-jdbc-3.2.0-1.19.jar" \
  "$DIR/flink-connector-jdbc-3.2.0-1.19.jar"

# MySQL 官方 JDBC 驱动。
download "https://repo.maven.apache.org/maven2/com/mysql/mysql-connector-j/8.0.33/mysql-connector-j-8.0.33.jar" \
  "$DIR/mysql-connector-j-8.0.33.jar"

echo "done -> $DIR"
ls -lh "$DIR"
