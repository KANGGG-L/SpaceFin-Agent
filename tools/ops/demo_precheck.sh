#!/usr/bin/env bash
# demo_precheck.sh —— 演示前一键自检（T-1 天与开演前 10 分钟各跑一次）
#
# 用法：
#   tools/ops/demo_precheck.sh           # 全量检查（含隧道端到端实测，约 40 秒）
#   tools/ops/demo_precheck.sh --fast    # 跳过隧道实测（只查本机状态）
#
# 退出码：0=全部通过；1=存在 FAIL（演示大概率翻车项）；WARN 不阻断但要心里有数。

set -uo pipefail

FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

PORT="${DEMO_PORT:-8500}"
TARGET="http://127.0.0.1:${PORT}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

n_pass=0 n_warn=0 n_fail=0
ok()   { printf '  [PASS] %s\n' "$1"; n_pass=$((n_pass+1)); }
warn() { printf '  [WARN] %s —— %s\n' "$1" "$2"; n_warn=$((n_warn+1)); }
fail() { printf '  [FAIL] %s —— %s\n' "$1" "$2"; n_fail=$((n_fail+1)); }

echo "== 1. 工具与依赖 =="
if command -v cloudflared >/dev/null 2>&1; then
  ok "cloudflared 已安装（$(cloudflared --version 2>/dev/null | head -1)）"
else
  fail "cloudflared 缺失" "跑 tools/ops/cloudflared_demo.sh 会提示安装命令"
fi
command -v curl >/dev/null 2>&1 && ok "curl 可用" || fail "curl 缺失" "隧道检查依赖 curl"

echo
echo "== 2. 数据栈（Docker） =="
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^spacefin-mysql$'; then
  ok "spacefin-mysql 容器运行中"
else
  fail "spacefin-mysql 未运行" "cd $REPO && make up && make health"
fi
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^spacefin-redis$'; then
  ok "spacefin-redis 容器运行中"
else
  warn "spacefin-redis 未运行" "核心演示页不强依赖，但 dev.sh 热重载需要"
fi

echo
echo "== 3. 关键产物（output/） =="
# AVM 模型必须是真实文件而非 LFS 指针文本
MJ="$REPO/output/avm/model.joblib"
if [ -f "$MJ" ] && [ "$(stat -c%s "$MJ")" -gt 1000000 ]; then
  ok "model.joblib 为真实模型（$(du -h "$MJ" | cut -f1)）"
elif [ -f "$MJ" ]; then
  fail "model.joblib 疑似 LFS 指针文本" "git lfs install && git lfs pull"
else
  warn "model.joblib 缺失" "P7 AVM 页会显示产物缺失空态；可先跑 tools/avm/train.py"
fi
for f in output/avm/avm_report.json output/spatial/spatial_report.json output/risk/dws_risk_class.csv; do
  if [ -f "$REPO/$f" ]; then ok "$f 存在"; else warn "$f 缺失" "对应页面显示空态（不崩溃）；要完整数据跑 tools/dev/backfill_7d.py --all"; fi
done

echo
echo "== 4. 前端服务 =="
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$TARGET/" 2>/dev/null || true)"
if [ "$code" = "200" ]; then
  ok "前端 ${TARGET} 响应 200"
elif [ -z "$code" ] || [ "$code" = "000" ]; then
  fail "前端未启动" "SF_DEV_MODE=1 python3 tools/frontend/app.py --host 127.0.0.1 --port ${PORT}"
else
  warn "前端 ${TARGET} 返回 HTTP $code" "非 200，检查 app.py 日志"
fi
if [ -f "$REPO/output/frontend/credentials.json" ]; then
  ok "登录口令文件存在（credentials.json）"
else
  warn "无 credentials.json" "dev 模式（SF_DEV_MODE=1）用固定口令 admin/admin，可忽略"
fi

echo
echo "== 5. 隧道端到端 =="
if [ "$FAST" = "1" ]; then
  warn "已跳过（--fast）" "开演前建议完整跑一次"
elif [ "$n_fail" -gt 0 ]; then
  warn "因前面存在 FAIL 而跳过" "先修复再验证隧道"
else
  TMP="$(mktemp)"
  cloudflared tunnel --url "$TARGET" --no-autoupdate >"$TMP" 2>&1 &
  trap 'kill %1 2>/dev/null; rm -f "$TMP"' EXIT
  URL=""
  for _ in $(seq 1 60); do
    URL="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$TMP" | head -1 || true)"
    [ -n "$URL" ] && break
    sleep 0.5
  done
  if [ -z "$URL" ]; then
    fail "30 秒内未拿到 trycloudflare URL" "查看 $TMP 日志，常见为出网受限"
  else
    tcode="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL/" 2>/dev/null || true)"
    if [ "$tcode" = "200" ]; then
      ok "公网经隧道访问返回 200：$URL"
    else
      fail "隧道 URL 访问异常（HTTP ${tcode:-无响应}）" "$URL —— 检查 cloudflared 日志"
    fi
  fi
  kill %1 2>/dev/null
fi

echo
echo "================ 汇总：PASS=$n_pass WARN=$n_warn FAIL=$n_fail ================"
if [ "$n_fail" -gt 0 ]; then
  echo "存在 FAIL 项，按提示修复后重跑本脚本。"
  exit 1
fi
echo "可以演示。开演前 10 分钟执行：tools/ops/cloudflared_demo.sh（URL 会变化，现开现发）"
