#!/usr/bin/env bash
# 前端逐页冒烟测试：动态发现导航项，逐页点击并捕获 console 错误（带 runtime 重试）
set -u
BASE=http://127.0.0.1:8500/

orca_eval() {
  local expr="$1" attempt out
  for attempt in 1 2 3 4 5; do
    out=$(orca-ide eval --expression "$expr" --json 2>&1)
    echo "$out" | grep -q '"ok": true' && { echo "$out"; return 0; }
    sleep 2
  done
  echo "$out"
  return 1
}

orca_console() {
  local attempt out
  for attempt in 1 2 3 4 5; do
    out=$(orca-ide console --limit 1000 --json 2>&1)
    echo "$out" | grep -q '"ok": true' && { echo "$out"; return 0; }
    sleep 2
  done
  echo "$out"
  return 1
}

orca-ide tab create --url "$BASE" --json >/dev/null 2>&1
orca-ide wait --load networkidle --json >/dev/null 2>&1
sleep 1

# 动态获取导航页 id
IDS=$(orca_eval "JSON.stringify([...document.querySelectorAll('#nav a[data-page]')].map(a=>a.dataset.page))" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); v=d.get('result',{}).get('result'); print(' '.join(json.loads(v)) if v else '')" 2>/dev/null)

echo "发现导航页: $IDS"
echo "=== 开始逐页测试 ==="

TOTAL_MSGS=0
TOTAL_ERRS=0

for id in $IDS; do
  orca_eval "document.querySelector('#nav a[data-page=\"$id\"]').click()" >/dev/null 2>&1
  orca-ide wait --load networkidle --json >/dev/null 2>&1
  sleep 1.5
  MSG=$(orca_console)
  # 解析：统计总消息与错误，并打印每条错误
  READOUT=$(echo "$MSG" | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    msgs=d.get('result',{}).get('messages',[])
except Exception:
    msgs=[]
errs=[m for m in msgs if (m.get('type') in ('error','warning') or (str(m.get('level','')).lower() in ('error','severe')))]
print(len(msgs), len(errs))
for m in errs:
    keys=list(m.keys())
    txt=str(m.get('text') or m.get('message') or m)
    print('  >>', txt[:200])
" 2>/dev/null)
  read -r nm ne <<< "$READOUT"
  nm=${nm:-0}; ne=${ne:-0}
  TOTAL_MSGS=$((TOTAL_MSGS + nm))
  TOTAL_ERRS=$((TOTAL_ERRS + ne))
  echo "[$id] msgs=$nm errors/warnings=$ne"
done

echo "=== 汇总: 总消息=$TOTAL_MSGS 错误/警告=$TOTAL_ERRS ==="
if [ "$TOTAL_ERRS" -gt 0 ]; then echo "RESULT: FAIL (有错误)"; else echo "RESULT: PASS (无 console 错误/警告)"; fi

# 清理：关闭本次创建的 tab，避免累积导致 runtime_unavailable
orca-ide tab close --json >/dev/null 2>&1 || true
