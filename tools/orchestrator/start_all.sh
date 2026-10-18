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
# shellcheck source=tools/orchestrator/lib_host_repo_root.sh
. "$(dirname "$0")/lib_host_repo_root.sh"
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
  # 同时探 127.0.0.1（宿主机直装 Airflow）与 host.docker.internal
  # （容器化部署：渲染服务跑在宿主 WSL，容器经 host.docker.internal 访问，
  # 其 127.0.0.1 回环上没有该服务）。二者任一可达即视为健康。
  if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$RENDER_PORT/health"; then return 0; fi
  if curl -s -o /dev/null --max-time 3 "http://host.docker.internal:$RENDER_PORT/health"; then return 0; fi
  if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$RENDER_PORT/"; then return 0; fi
  if curl -s -o /dev/null --max-time 3 "http://host.docker.internal:$RENDER_PORT/"; then return 0; fi
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
      # 优先 systemd（unit 已装时）；unit 缺失 / systemd 不可用 → 直接后台拉
      # host_render_service.py。⚠️ 回退必须真正落到直启分支：此前 user bus 可达但 unit
      # 未安装时，`|| echo "fallback to direct launch"` 只打印不回退，导致渲染服务
      # 根本没起（render_alive 30s 探测失败 → start_stack 失败 / 要人工 nohup）。
      _direct_launch() {
        if [ -x "$VENV/bin/python" ]; then
          # 容器内 airflow 用户对仓库 output/ 无写权限，日志改写到可写的 /tmp。
          _fallback_log="/tmp/spacefin-render/host_render_service.log"
          mkdir -p "$(dirname "$_fallback_log")"
          echo "[start_all] launching host_render_service.py directly in background"
          nohup "$VENV/bin/python" "$REPO_ROOT/tools/orchestrator/host_render_service.py" "$RENDER_PORT" \
            > "$_fallback_log" 2>&1 &
          echo "[start_all] render service launched (pid $!) -> $_fallback_log"
        else
          echo "[start_all] venv missing ($VENV): cannot launch render service" >&2
          return 1
        fi
      }
      if systemctl --user show-environment >/dev/null 2>&1 \
         && systemctl --user list-unit-files "$SERVICE" 2>/dev/null | grep -q "$SERVICE"; then
        echo "[start_all] starting host render service via systemd --user ($SERVICE) ..."
        systemctl --user start "$SERVICE" 2>&1 || _direct_launch
      elif sudo -n systemctl start "$SERVICE" 2>/dev/null; then
        echo "[start_all] starting host render service via sudo systemctl ($SERVICE) ..."
        :
      else
        _direct_launch
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
  # bind 源必须是 dockerd 可见的绝对路径：本脚本常由 Airflow 在 airflow 容器内执行，
  # 届时 $REPO_ROOT=/opt/spacefin，沿用相对路径会让 dockerd 另建一棵树、采集结果丢失。
  spacefin_export_host_repo_root "$REPO_ROOT" || exit 1
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
