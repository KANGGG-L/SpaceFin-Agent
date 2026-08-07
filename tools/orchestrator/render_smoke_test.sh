#!/bin/bash
# 宿主 fangyuan 渲染服务冒烟测试（Airflow DAG 的第一个任务用它做前置门禁）。
#
# 为什么需要它：容器内 Chrome 会被站点反爬按指纹软拦截（只回空心壳、0 卡片），
# 迁到新机后必须先确认「宿主 Chrome 能真拿到 zu-itemmod 卡片」，否则整轮 fangyuan 空转。
#
# 两种模式（日常门禁 ≠ 新机验收，务必分清）：
#   SMOKE_REQUIRE_RENDER=0（默认，门禁模式）
#     契约 §4 规定任务 1 只是探活 {render_url}/health：/health 不通 → exit 1。
#     后面的真实渲染只是顺带做的深度检查，拿不到代理 / 渲染失败一律 WARN + exit 0。
#     必须这样，否则整条日调度会自锁：本脚本在 DAG 里排在 start_stack 之前，此刻
#     master 容器可能压根没起（新机首跑、前一日收尾停机），即使在跑也仍是上一 run 的
#     状态（重置 ip_used 的 _bootstrap_run 要等 start_stack 才执行），预算跑满就永远
#     取不到代理。另外渲染服务是 5 槽阻塞队列，幂等重跑时槽被生产流量占满会排队超时，
#     门禁不该与生产渲染抢槽、更不该因此判失败。
#   SMOKE_REQUIRE_RENDER=1（验收模式）
#     必须真拿到 zu-itemmod 卡片才 exit 0。迁到新机后的第一件事就该用这个模式手工跑一次。
#
# 接口按 tools/orchestrator/host_render_service.py 实际实现：
#   GET /health                                  → {"status":"ok","slot_held_sec":..,"slots":..}
#   GET /render?city=&page=&proxy=&auth=         → 200 HTML；缺 proxy 返回 403（服务硬禁直连宿主 IP）
#
# 用法：bash tools/orchestrator/render_smoke_test.sh
#      新机验收：SMOKE_REQUIRE_RENDER=1 SMOKE_PROXY=1.2.3.4:8000 SMOKE_PROXY_AUTH=1 bash ...
# 环境变量：
#   RENDER_URL           默认 http://127.0.0.1:8899
#   SMOKE_CITY           默认 sz
#   SMOKE_PAGE           默认 1
#   SMOKE_PROXY          "ip:port"；不给则向 master 要一个（渲染服务禁止直连，必须带代理）
#   SMOKE_PROXY_AUTH     0/1，青果代理需 1（向 master 取时按 source 自动判定）
#   MASTER_URL           默认 http://127.0.0.1:5100（仅在 SMOKE_PROXY 未给时使用）
#   SMOKE_REQUIRE_RENDER 0/1，默认 0；1 = 渲染必须成功，否则 exit 1
#   SMOKE_RENDER_TIMEOUT 渲染请求超时秒数，默认 180
# 退出码：0 = 门禁通过（或验收模式下拿到 zu-itemmod 卡片）；1 = 服务不可达 / 验收失败
set -euo pipefail

RENDER_URL="${RENDER_URL:-http://127.0.0.1:8899}"
SMOKE_CITY="${SMOKE_CITY:-sz}"
SMOKE_PAGE="${SMOKE_PAGE:-1}"
MASTER_URL="${MASTER_URL:-http://127.0.0.1:5100}"
SMOKE_PROXY="${SMOKE_PROXY:-}"
SMOKE_PROXY_AUTH="${SMOKE_PROXY_AUTH:-0}"
SMOKE_REQUIRE_RENDER="${SMOKE_REQUIRE_RENDER:-0}"
SMOKE_RENDER_TIMEOUT="${SMOKE_RENDER_TIMEOUT:-180}"

BODY="$(mktemp -t spacefin_smoke.XXXXXX)"
trap 'rm -f "$BODY"' EXIT

# 深度检查（取代理 + 真实渲染）失败时的收尾：门禁模式放行，验收模式失败。
end_soft() {
  if [ "$SMOKE_REQUIRE_RENDER" = "1" ]; then
    echo "[smoke] FAIL: SMOKE_REQUIRE_RENDER=1 要求渲染必须成功" >&2
    exit 1
  fi
  echo "[smoke] PASS(gate): /health 可达即视为门禁通过；深度渲染检查未通过，仅告警" >&2
  exit 0
}

# 1) 服务存活（唯一的硬门禁，契约 §4）
if ! curl -s -o /dev/null --max-time 5 "$RENDER_URL/health"; then
  echo "[smoke] FAIL: render service not reachable at $RENDER_URL/health" >&2
  exit 1
fi
echo "[smoke] health ok: $(curl -s --max-time 5 "$RENDER_URL/health")"

# 2) 代理（渲染服务硬禁直连宿主 IP，无 proxy 直接 403）
if [ -z "$SMOKE_PROXY" ]; then
  # 刻意不带 city/type：契约 §3.3 规定缺省时不计预算，避免门禁把该城 qg 配额吃掉
  resp="$(curl -s --max-time 10 "$MASTER_URL/proxy/random" || true)"
  SMOKE_PROXY="$(printf '%s' "$resp" | sed -n 's/.*"proxy"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  src="$(printf '%s' "$resp" | sed -n 's/.*"source"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  if [ "$src" = "qg" ]; then SMOKE_PROXY_AUTH=1; fi
  if [ -z "$SMOKE_PROXY" ]; then
    echo "[smoke] WARN: no proxy available（master 未起 / 池空 / 预算耗尽），跳过真实渲染" >&2
    echo "[smoke]   master $MASTER_URL said: ${resp:-<empty>}" >&2
    echo "[smoke]   需要深度检查时请显式指定 SMOKE_PROXY" >&2
    end_soft
  fi
  echo "[smoke] got proxy from master: $SMOKE_PROXY (source=${src:-?}, auth=$SMOKE_PROXY_AUTH)"
fi

# 3) 真实渲染一页
url="$RENDER_URL/render?city=$SMOKE_CITY&page=$SMOKE_PAGE&proxy=$SMOKE_PROXY&auth=$SMOKE_PROXY_AUTH"
code="$(curl -s --max-time "$SMOKE_RENDER_TIMEOUT" -o "$BODY" -w '%{http_code}' "$url" || echo "000")"
size="$(wc -c <"$BODY" | tr -d ' ')"

if grep -q 'zu-itemmod' "$BODY"; then
  cards="$(grep -o 'zu-itemmod' "$BODY" | wc -l | tr -d ' ')"
  echo "[smoke] PASS: $SMOKE_CITY p$SMOKE_PAGE http=$code bytes=$size zu-itemmod=$cards"
  exit 0
fi

echo "[smoke] WARN: no zu-itemmod in response ($SMOKE_CITY p$SMOKE_PAGE)" >&2
echo "[smoke]   http_code=$code bytes=$size proxy=$SMOKE_PROXY auth=$SMOKE_PROXY_AUTH" >&2
if grep -q 'anjukestatic' "$BODY"; then
  echo "[smoke]   anjukestatic=yes → 是安居客真实页面但无卡片（页码越界/该城无数据？）" >&2
else
  echo "[smoke]   anjukestatic=no  → 不是安居客页面（验证码墙 / 代理故障 / Chrome 错误页）" >&2
fi
if grep -q '请输入验证码' "$BODY"; then
  echo "[smoke]   captcha=yes（出口 IP 被 58 反爬弹码）" >&2
fi
if grep -q '404-安居客' "$BODY"; then
  echo "[smoke]   site_404=yes（页码超出该城真实页深）" >&2
fi
if [ "$code" = "000" ]; then
  echo "[smoke]   000 = ${SMOKE_RENDER_TIMEOUT}s 内无响应（5 槽被生产渲染占满时会排队，属正常）" >&2
elif [ "$code" = "403" ]; then
  echo "[smoke]   403 = 渲染服务拒绝直连（proxy 参数缺失/为空）" >&2
elif [ "$code" = "500" ]; then
  echo "[smoke]   500 = 渲染异常，见 output/guangdong/logs/host_render_service.log" >&2
fi
head -c 300 "$BODY" >&2
echo >&2
end_soft
