#!/bin/bash
# SpaceFin 采集编排一键启动：宿主 fangyuan 渲染服务（launchd / systemd 托管）+ docker compose 全栈
#   - 宿主渲染服务（tools/orchestrator/host_render_service.py）：
#     宿主 Chrome + 源 chrome_profile，容器 worker 经 host.docker.internal 调用
#     （容器内 Chrome 会被站点反爬按指纹软拦截，宿主 Chrome 已验证可正常出数）
#     macOS 由 launchd 托管（plist: tools/orchestrator/com.spacefin.host-render.plist）
#     Linux  由 systemd 托管（unit: deploy/systemd/spacefin-host-render.service）
#   - 容器栈：master 主备 + worker-1..5（up -d 前先 build 全部服务镜像，保证镜像一致；
#     改过 master.py 只 build worker 会留下陈旧 master 镜像，这是踩过的坑）
# 用法：
#   bash tools/orchestrator/start_all.sh                    # 全量启动
#   bash tools/orchestrator/start_all.sh --render-only      # 仅启动宿主渲染服务（不碰容器）
#   bash tools/orchestrator/start_all.sh --no-build         # 跳过 docker build，直接 up -d
#   bash tools/orchestrator/start_all.sh --allow-degraded   # 渲染服务不健康时不退出（人工场景）
# 环境变量：SPACEFIN_RENDER_VENV（默认 <repo>/tools/orchestrator/.venv）、RENDER_PORT（默认 8899）、
#           CRAWL_RUN_ID（默认 manual，透传给 compose；Airflow 传 {{ ds }}）
# 退出码：渲染服务健康检查失败 → 1（不再只打 WARNING，避免 Airflow「假成功」后 fangyuan 全程无数据）
set -euo pipefail
cd "$(dirname "$0")/../.."

REPO_ROOT="$(pwd)"
VENV="${SPACEFIN_RENDER_VENV:-$REPO_ROOT/tools/orchestrator/.venv}"
RENDER_PORT="${RENDER_PORT:-8899}"
RENDER_LOG="output/guangdong/logs/host_render_service.log"
PLIST="tools/orchestrator/com.spacefin.host-render.plist"
LABEL="com.spacefin.host-render"
SERVICE="spacefin-host-render.service"
COMPOSE_FILE="tools/orchestrator/docker-compose.yml"
BUILD_TARGETS="master-primary master-standby worker-1 worker-2 worker-3 worker-4 worker-5"

RENDER_ONLY=0
ALLOW_DEGRADED=0
DO_BUILD=1
for arg in "$@"; do
  case "$arg" in
    --render-only) RENDER_ONLY=1 ;;
    --allow-degraded) ALLOW_DEGRADED=1 ;;
    --no-build) DO_BUILD=0 ;;
    *) echo "[start_all] unknown option: $arg" >&2; exit 2 ;;
  esac
done

# CRAWL_RUN_ID 透传给 compose（compose 侧用 ${CRAWL_RUN_ID:-manual}）；不覆盖用户已设置的值
export CRAWL_RUN_ID="${CRAWL_RUN_ID:-manual}"

render_alive() {
  if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$RENDER_PORT/health"; then return 0; fi
  if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$RENDER_PORT/"; then return 0; fi
  return 1
}

# 1) 宿主渲染服务 Python 环境（本机为 conda `spark` 别名 .venv；缺才重建）
if [ ! -x "$VENV/bin/python" ]; then
  echo "[start_all] creating venv $VENV (DrissionPage + curl_cffi) ..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q DrissionPage curl_cffi
fi

# 2) 宿主渲染服务（幂等：已在听则跳过；否则按平台用 launchd / systemd 拉起）
mkdir -p output/guangdong/logs
if render_alive; then
  echo "[start_all] host render service already listening on :$RENDER_PORT"
else
  case "$(uname -s)" in
    Darwin)
      echo "[start_all] loading host render service via launchd ($PLIST) ..."
      if ! launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
        # 已 bootstrapped 或旧版 macOS：退回到 load，再 kickstart 兜底
        launchctl load "$PLIST" 2>/dev/null || true
      fi
      launchctl kickstart "gui/$(id -u)/$LABEL" 2>/dev/null || true
      ;;
    Linux)
      echo "[start_all] starting host render service via systemd ($SERVICE) ..."
      if systemctl --user show-environment >/dev/null 2>&1; then
        systemctl --user start "$SERVICE" 2>&1 || \
          echo "[start_all] systemctl --user start $SERVICE failed" >&2
      elif sudo -n systemctl start "$SERVICE" 2>&1; then
        :
      else
        echo "[start_all] systemctl start $SERVICE failed (user bus unavailable, sudo -n denied?)" >&2
      fi
      ;;
    *)
      echo "[start_all] unsupported platform $(uname -s): start host_render_service.py manually" >&2
      ;;
  esac
  ok=0
  for _ in $(seq 1 30); do
    if render_alive; then ok=1; break; fi
    sleep 1
  done
  if [ "$ok" -ne 1 ]; then
    if [ "$ALLOW_DEGRADED" -eq 1 ]; then
      echo "[start_all] WARNING: render service not healthy after 30s (--allow-degraded); check $RENDER_LOG" >&2
    else
      echo "[start_all] ERROR: render service not healthy after 30s; check $RENDER_LOG" >&2
      echo "[start_all] fangyuan 渲染不可用会导致整轮无出租数据，直接失败退出" >&2
      exit 1
    fi
  fi
fi

# 3) 容器栈（--render-only 时跳过）
if [ "$RENDER_ONLY" -ne 1 ]; then
  if [ "$DO_BUILD" -eq 1 ]; then
    echo "[start_all] docker compose build $BUILD_TARGETS ..."
    # shellcheck disable=SC2086
    docker compose --env-file .env -f "$COMPOSE_FILE" build $BUILD_TARGETS
  else
    echo "[start_all] --no-build: skip docker compose build"
  fi
  echo "[start_all] docker compose up -d (CRAWL_RUN_ID=$CRAWL_RUN_ID) ..."
  docker compose --env-file .env -f "$COMPOSE_FILE" up -d
fi

echo "[start_all] done (render service :$RENDER_PORT, run_id=$CRAWL_RUN_ID)"
