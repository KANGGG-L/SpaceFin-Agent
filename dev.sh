#!/usr/bin/env bash
# SpaceFin 本地开发一键拉起（带热重载）。不依赖 make（本机 WSL 可能没装 make）。
#
# 做三件事：
#   1) 加载 .env（与 Makefile 行为一致）
#   2) 确保基础设施：MySQL（spacefin-mysql）+ Redis（spacefin-redis）已在 127.0.0.1 起好
#   3) exec dev_server.py —— 前端(:8500) + 后端 master(:5102) 带热重载
#
# 设计：原生后端 master 默认跑 5102，避开 Docker orchestrator 栈里 master-primary 占用的 5100；
#       并连 Redis db=1，与 Docker master（db=0）隔离，互不抢主备选举锁。
#       Docker 采集栈可继续独立运行，不受影响。
#
# 用法：
#   ./dev.sh                         # 拉起前后端 + 热重载
#   ./dev.sh --backend-port 5102     # 透传参数给 dev_server.py
#   ./dev.sh --no-backend           # 只拉前端
set -euo pipefail
cd "$(dirname "$0")"

# 1) 加载 .env
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

# 2) 基础设施（Docker 不可用时仅告警，不阻断前后端拉起——便于纯前端/RBAC 调试）
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "[dev.sh] WARN: Docker 不可用（Docker Desktop 未运行/未集成 WSL）。"
  echo "[dev.sh] WARN: 前端可正常启动并热重载；依赖 MySQL/Redis 的功能（DB 查询、后端选主）暂不可用。"
  echo "[dev.sh] WARN: 请在 Windows 侧启动 Docker Desktop 后重试以获得完整能力。"
else
  echo "[dev.sh] ensuring MySQL (spacefin-mysql) ..."
  docker start spacefin-mysql >/dev/null 2>&1 || docker compose up -d mysql || \
    echo "[dev.sh] WARN: MySQL 拉起失败，DB 相关功能不可用。"

  echo "[dev.sh] ensuring Redis (spacefin-redis) ..."
  docker network create spacefin-net >/dev/null 2>&1 || true
  docker start spacefin-redis >/dev/null 2>&1 || \
    docker run -d --name spacefin-redis --network spacefin-net -p 6379:6379 redis:7 redis-server --appendonly yes >/dev/null 2>&1 || true

  # 探活 Redis（最多等 20s）
  for _ in $(seq 1 20); do
    if docker exec spacefin-redis redis-cli ping 2>/dev/null | grep -q PONG; then
      echo "[dev.sh] Redis ready"; break
    fi
    sleep 1
  done
fi

# 3) 启动热重载监督器
# 选解释器：优先 orchestrator venv（含 redis/pymysql/curl_cffi）。
# 本机 PATH 上的 python3 是 conda base，缺 redis 模块，用它拉后端会 ImportError 崩溃。
PY="$(cd "$(dirname "$0")" && pwd)/tools/orchestrator/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
echo "[dev] python -> $PY"
exec "$PY" dev_server.py "$@"
