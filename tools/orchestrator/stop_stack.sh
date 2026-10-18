#!/bin/bash
# SpaceFin 采集栈一键停止：docker compose stop（保留容器，不 down）
#
# 为什么需要它：start_all.sh 拉起采集栈（master 主备 + worker-1..5）后从来不停，
# 7 个容器天天驻留叠加，把 7.7G 内存的机器拖到 OOM。本脚本由 DAG 的 stop_crawl_stack
# 任务在 drain_workers 之后调用，爬完即关；内存吃紧时也可脱离 Airflow 手工急救。
#
# 为什么用 stop 而不是 down：
#   - down 会删容器，连带丢掉整夜 docker logs（爬取失败时唯一的现场）；
#   - stop 保留容器层与 ID，次日 start_all.sh 的 up -d 是「启动」而非「重建」，秒起；
#   - 7 个服务都是 restart: unless-stopped，手动 stop 后不会自启；
#   - 改过代码要出新镜像时不受影响：start_all.sh 先 build，up -d 检测镜像变化照样 recreate。
#
# 为什么不加 -p：project 名由 -f 所在目录推导为 orchestrator，与启动侧 start_all.sh
# 完全一致，才能命中 orchestrator-worker-*-1 和两个显式 container_name 的 master。
#
# 用法：
#   bash tools/orchestrator/stop_stack.sh            # best-effort：失败只告警，exit 0
#   bash tools/orchestrator/stop_stack.sh --strict   # 失败 exit 1（Variable
#                                                     #  spacefin_stop_stack_strict=1 时由 DAG 传入）
# 环境变量：SPACEFIN_ALERT_SLACK_WEBHOOK / SPACEFIN_ALERT_MENTIONS（留空则跳过通知）
# 退出码：0 = 已停 / 无容器可停 / best-effort 下失败；1 = --strict 下失败
# 注意：刻意不 set -e —— best-effort 语义要自己控退出码。
set -uo pipefail
cd "$(dirname "$0")/../.."

COMPOSE_FILE="tools/orchestrator/docker-compose.yml"
# shellcheck source=tools/orchestrator/lib_host_repo_root.sh
. "$(dirname "$0")/lib_host_repo_root.sh"
STRICT=0
for arg in "$@"; do
  case "$arg" in
    --strict) STRICT=1 ;;
    *) echo "[stop_stack] unknown option: $arg" >&2; exit 2 ;;
  esac
done

# --env-file 仅在实际存在时带上：换机部署没 .env 时不该让停栈这一步先挂掉
ENV_ARGS=()
if [ -f .env ]; then
  ENV_ARGS=(--env-file .env)
fi

fail() {
  # best-effort 失败必须留下可见痕迹：默认不阻断链路，只能靠 Slack 补可见性
  echo "[stop_stack] ERROR: $*" >&2
  if [ -n "${SPACEFIN_ALERT_SLACK_WEBHOOK:-}" ]; then
    python3 -c '
import os, sys
sys.path.insert(0, "tools/orchestrator")
from slack_notify import build_alert_message, post_slack
post_slack(
    os.getenv("SPACEFIN_ALERT_SLACK_WEBHOOK", ""),
    build_alert_message(
        "stop_crawl_stack",
        os.getenv("SPACEFIN_DS", "-"),
        "开发",
        "采集栈停止失败：容器可能仍在驻留占用内存（best-effort，未阻断链路）。请手工执行 "
        "bash tools/orchestrator/stop_stack.sh",
        os.getenv("SPACEFIN_ALERT_MENTIONS", ""),
    ),
)
' || echo "[stop_stack] WARN: slack notify failed" >&2
  fi
  if [ "$STRICT" -eq 1 ]; then
    echo "[stop_stack] --strict: 失败即退出 1" >&2
    exit 1
  fi
  echo "[stop_stack] best-effort: 告警后放行，不阻断下游 ETL" >&2
  exit 0
}

if ! command -v docker >/dev/null 2>&1; then
  fail "docker CLI 不可用（Docker Desktop 未启动？）；采集栈可能仍在驻留"
fi

# compose 的 bind 源要 SPACEFIN_REPO_ROOT，缺它连 stop 都会在插值阶段报错，
# 故先解析（在容器内执行时同理，见 lib_host_repo_root.sh）。
spacefin_export_host_repo_root "$(pwd)" || fail "无法确定 dockerd 可见的仓库根，compose 插值失败"

echo "[stop_stack] docker compose stop ($COMPOSE_FILE) ..."
# ${arr[@]+...} 展开：老 bash（<4.4）在 set -u 下展开空数组会报 unbound variable
if ! docker compose ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} -f "$COMPOSE_FILE" stop; then
  fail "docker compose stop 返回非 0（daemon 不通 / socket 无权 / 服务不存在）"
fi

echo "[stop_stack] done: 采集栈已停（容器保留，日志不丢）"
