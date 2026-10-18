#!/usr/bin/env bash
# cloudflared_demo.sh —— 用 Cloudflare 快速隧道把本机前端临时暴露到公网，
# 方便其他用户远程访问演示，无需开放 VPS 端口或配置域名。
#
# 用法：
#   tools/ops/cloudflared_demo.sh                # 默认暴露 http://127.0.0.1:8500（前端）
#   tools/ops/cloudflared_demo.sh 8600           # 暴露其他端口
#   DEMO_PORT=8600 tools/ops/cloudflared_demo.sh
#
# 说明：
#   - 使用 cloudflared 免费 quick tunnel（*.trycloudflare.com 临时域名），无需 Cloudflare 账号；
#   - 每次启动 URL 随机变化，进程停止即失效——只适合临时演示，不适合长期部署；
#   - 前端登录口令由 app.py 启动时生成（output/frontend/credentials.json），请单独发给访客；
#   - 访客只应拿到本 URL；数据栈（MySQL/Doris 等）仍只绑回环/内网，隧道不暴露它们。

set -euo pipefail

PORT="${1:-${DEMO_PORT:-8500}}"
TARGET="http://127.0.0.1:${PORT}"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "错误：未找到 cloudflared。任选一种方式安装后重试："
  echo "  # 方式一：官方 deb 包（Debian/Ubuntu）"
  echo "  curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb && sudo dpkg -i /tmp/cloudflared.deb"
  echo "  # 方式二：单文件二进制放用户目录（免 sudo）"
  echo "  curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o ~/.local/bin/cloudflared && chmod +x ~/.local/bin/cloudflared"
  exit 1
fi

# 目标端口无响应时给出提醒（不阻断——隧道可先建，前端随后再起）
if ! curl -fsS -o /dev/null --max-time 2 "${TARGET}/healthz" 2>/dev/null \
  && ! curl -fsS -o /dev/null --max-time 2 "${TARGET}/" 2>/dev/null; then
  echo "提醒：${TARGET} 暂无响应。请确认前端已启动（dev 模式：SF_DEV_MODE=1 python3 tools/frontend/app.py）。"
fi

echo "正在通过 Cloudflare 快速隧道暴露 ${TARGET}（Ctrl-C 停止）..."
echo

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

cloudflared tunnel --url "${TARGET}" --no-autoupdate 2>&1 | tee "$LOG" &

# 等待 quick tunnel 域名分配（最多 30 秒），从日志提取 URL 醒目展示
URL=""
for _ in $(seq 1 60); do
  URL="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
  [ -n "$URL" ] && break
  sleep 0.5
done

if [ -n "$URL" ]; then
  echo
  echo "==================== 远程访问 URL ===================="
  echo "  $URL"
  echo "======================================================"
  echo "把该 URL 发给访客即可；本进程停止后 URL 立即失效。"
else
  echo "30 秒内未解析到 trycloudflare URL，请查看上方 cloudflared 日志。"
fi

wait
