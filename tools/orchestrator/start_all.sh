#!/bin/bash
# SpaceFin 采集编排一键启动：宿主 fangyuan 渲染服务（launchd 托管）+ docker compose 全栈
#   - 宿主渲染服务（tools/orchestrator/host_render_service.py）：
#     宿主 Chrome 150 + 源 chrome_profile，容器 worker 经 host.docker.internal 调用
#     （容器内 Chrome 151 会被站点反爬按指纹软拦截，宿主 150 已验证可正常出数）
#     由 launchd 托管（plist: tools/orchestrator/com.spacefin.host-render.plist，Label com.spacefin.host-render）
#   - 容器栈：master 主备 + worker-1..5（up -d 前先 build 全部 worker，保证镜像一致）
# 用法：
#   bash tools/orchestrator/start_all.sh                # 全量启动
#   bash tools/orchestrator/start_all.sh --render-only  # 仅启动宿主渲染服务（不碰容器）
# 环境变量：SPACEFIN_RENDER_VENV（默认 <repo>/tools/orchestrator/.venv）、RENDER_PORT（默认 8899）
set -euo pipefail
cd "$(dirname "$0")/../.."

REPO_ROOT="$(pwd)"
VENV="${SPACEFIN_RENDER_VENV:-$REPO_ROOT/tools/orchestrator/.venv}"
RENDER_PORT="${RENDER_PORT:-8899}"
RENDER_LOG="output/guangdong/logs/host_render_service.log"
PLIST="tools/orchestrator/com.spacefin.host-render.plist"
LABEL="com.spacefin.host-render"

# 1) 宿主渲染服务 venv（DrissionPage + curl_cffi，缺则重建）
if [ ! -x "$VENV/bin/python" ]; then
  echo "[start_all] creating venv $VENV (DrissionPage + curl_cffi) ..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q DrissionPage curl_cffi
fi

# 2) 宿主渲染服务（launchd 托管，幂等：端口已在听则跳过）
mkdir -p output/guangdong/logs
if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$RENDER_PORT/"; then
  echo "[start_all] host render service already listening on :$RENDER_PORT"
else
  echo "[start_all] loading host render service via launchd ($PLIST) ..."
  if ! launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
    # 已 bootstrapped 或旧版 macOS：退回到 load，再 kickstart 兜底
    launchctl load "$PLIST" 2>/dev/null || true
  fi
  launchctl kickstart "gui/$(id -u)/$LABEL" 2>/dev/null || true
  ok=0
  for _ in $(seq 1 30); do
    if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$RENDER_PORT/"; then ok=1; break; fi
    sleep 1
  done
  if [ "$ok" -ne 1 ]; then
    echo "[start_all] WARNING: render service not healthy after 30s; check $RENDER_LOG" >&2
  fi
fi

# 3) 容器栈（--render-only 时跳过）
if [ "${1:-}" != "--render-only" ]; then
  echo "[start_all] docker compose build worker-1..5 ..."
  docker compose --env-file .env -f tools/orchestrator/docker-compose.yml \
    build worker-1 worker-2 worker-3 worker-4 worker-5
  echo "[start_all] docker compose up -d ..."
  docker compose --env-file .env -f tools/orchestrator/docker-compose.yml up -d
fi

echo "[start_all] done (render service :$RENDER_PORT)"
