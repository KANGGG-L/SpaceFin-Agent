#!/usr/bin/env bash
# manage.sh —— SpaceFin 资源管家：容器 + systemd 服务 + 内存水位监控
#
# 为什么做这个脚本：
#   主机只有 15G/4 核，但 Doris/前端/CDC 全常驻会吃紧；
#   之前服务用 nohup 裸跑，会话退出即丢，已发生前端掉线。
#   本脚本把「容器 + 用户级 systemd 服务 + 内存」收口到一个命令，
#   内存吃紧时可以一键停掉可降级层（t1，约释放 4.7G），避免 OOM。
#
# 三级运行策略：
#   T0 恒驻   —— 链路命脉，停机=业务中断：MySQL/Redis/CDC/Consumer/Airflow/前端
#   T1 可降级 —— 分析/存储层，可整层停：Doris/Minio（精简版已移除 Kafka/Flink 实时层与 stream-producer）
#   T2 按需   —— 任务型组件（爬虫/离线渲染），用完即弃，脚本不管理
#
# 组件清单：
#   t0: 容器 spacefin-mysql spacefin-redis
#       服务 spacefin-cdc spacefin-cdc-consumer airflow-scheduler airflow-webserver spacefin-frontend
#   t1: 容器 spacefin-doris-fe spacefin-doris-be spacefin-minio
#       （精简版已移除 spacefin-kafka / flink-* / spacefin-stream-producer 实时层）
#
# 依赖约定：所有容器 restart policy 已统一为 unless-stopped（宿主重启自动拉起）；
#           服务均托管为用户级 systemd unit（enable + linger 常驻）。
set -euo pipefail

# 用户级 systemd 需要 XDG_RUNTIME_DIR；cron/非交互 shell 里可能没带，这里兜底。
# 不兜底的话 systemctl --user 会报 "Failed to connect to bus"。
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# 告警阈值：可用内存低于该值(MiB)视为吃紧，提示停 t1 层
WARN_AVAIL_MB=2560

# 返回 tier 对应的容器名列表；非法 tier 返回非零，触发调用方报错
tier_containers() {
    case "$1" in
        t0) echo "spacefin-mysql spacefin-redis" ;;
        t1) echo "spacefin-doris-fe spacefin-doris-be spacefin-minio" ;;
        *) return 1 ;;
    esac
}

# 返回 tier 对应的用户级 systemd 服务列表
tier_services() {
    case "$1" in
        t0) echo "spacefin-cdc spacefin-cdc-consumer airflow-scheduler airflow-webserver spacefin-frontend" ;;
        t1) echo "spacefin-stream-producer" ;;
        *) return 1 ;;
    esac
}

# 可用内存(MiB)。free -m 的 Mem 行第 7 列是 available：
# 它是系统按可回收性估算出的"真可用"，比 free 列更接近实际可分配量。
mem_avail_mb() {
    free -m | awk '/^Mem:/ {print $7}'
}

# 是否处于采集窗口（Asia/Shanghai 00:30~04:00，DAG 每日爬取时段）。
# 期间采集集群 + 渲染进程是内存大头，T1 实时层/重任务应避让：
# 资源有限（15G）时爬虫与实时链同时满载有 OOM 风险，优先保数据源。
crawl_window() {
    local hhmm
    hhmm=$(TZ=Asia/Shanghai date '+%H%M')
    # 00:30 => 0030，04:00 => 0400
    if (( 10#$hhmm >= 0030 && 10#$hhmm <= 0400 )); then
        return 0  # 在采集窗口内
    fi
    return 1
}

# 重任务前置检查：启动 T1 层（或未来接管的重量级任务）前调用。
# ① 内存水位低于阈值 → 警告并提示降级/延后；
# ② 处于采集窗口 → 提示避让（采集优先于实时展示，前端此时以静态数据兜底）。
preflight() {
    local avail
    avail=$(mem_avail_mb)
    echo "[manage] preflight: available=${avail}MiB (阈值 ${WARN_AVAIL_MB}MiB)"
    if (( avail < WARN_AVAIL_MB )); then
        echo "[告警] 可用内存 < ${WARN_AVAIL_MB}MiB，启动 T1 有 OOM 风险。" >&2
        echo "       建议: 先 $0 stop t1 以外的重量级任务，或延后启动；" >&2
        echo "       确需启动请降低配额或加 swap（本机已配 4G swap 兜底）。" >&2
    fi
    if crawl_window; then
        echo "[提示] 当前处于采集窗口（Asia/Shanghai 00:30~04:00）。" >&2
        echo "       采集爬虫优先占用资源；实时链启动后前端以静态数据兜底，" >&2
        echo "       若内存告警可 $0 stop t1 让位采集。" >&2
    fi
}

cmd_status() {
    echo "=== 容器 (docker) ==="
    docker ps --format 'table {{.Names}}\t{{.Status}}'
    echo
    echo "=== 用户级 systemd 服务 ==="
    # grep 可能无匹配，用 || true 防止 set -e 提前退出
    systemctl --user list-units --type=service --no-pager | grep -E 'spacefin-|airflow-' || true
    echo
    echo "=== 内存水位 ==="
    free -h
}

# 启停一整个 tier。
# 为什么 start 先容器后服务、stop 先服务后容器：
#   服务(producer/cdc/consumer/前端)依赖数据库和队列先就绪，
#   先起容器再起服务能避免上游未就绪时的连接失败与反复重启；
#   反向停机则先把生产者/消费者拉下线，再停存储，避免写半截数据或刷告警。
tier_action() {
    local action="$1" tier="$2"
    local containers services
    containers=$(tier_containers "$tier") || { echo "未知 tier: $tier（支持 t0/t1）" >&2; return 1; }
    services=$(tier_services "$tier") || { echo "未知 tier: $tier（支持 t0/t1）" >&2; return 1; }

    if [ "$action" = start ]; then
        preflight
        echo "[manage] docker start ${tier}: $containers"
        # shellcheck disable=SC2086  # 变量按空格分词是刻意为之
        docker start $containers
        echo "[manage] systemctl --user start ${tier}: $services"
        # shellcheck disable=SC2086
        systemctl --user start $services
    else
        echo "[manage] systemctl --user stop ${tier}: $services"
        # shellcheck disable=SC2086
        systemctl --user stop $services
        echo "[manage] docker stop ${tier}: $containers"
        # shellcheck disable=SC2086
        docker stop $containers
        if [ "$tier" = t1 ]; then
            echo "[manage] t1 已停：释放约 4.7G 内存"
        fi
    fi
}

# 每 N 秒监控内存水位，低于阈值时往 stderr 告警（便于在管道/cron 里被捕获）。
cmd_watch() {
    local interval="${1:-30}"
    # 非交互守护场景下 stdout 可能被丢弃，告警必须走 stderr
    echo "[manage] watch 开始：每 ${interval}s 输出水位，available < ${WARN_AVAIL_MB}MiB 告警（Ctrl-C 退出）" >&2
    while true; do
        local avail
        avail=$(mem_avail_mb)
        printf '[%s] available=%dMiB\n' "$(date '+%F %T')" "$avail"
        if (( avail < WARN_AVAIL_MB )); then
            echo "[告警] 可用内存 < ${WARN_AVAIL_MB}MiB，建议执行: $0 stop t1（释放约 4.7G）" >&2
        fi
        sleep "$interval"
    done
}

# ---------------------------------------------------------------- 健康度探测
# 探测前端 /api/metrics（G8 新增）：需前端已启动且可登录拿到 token。
# 默认连 127.0.0.1:8500；可用环境变量覆盖：SPF_HOST / SPF_PORT / SPF_USER / SPF_PASS。
cmd_health() {
    local host="${SPF_HOST:-127.0.0.1}"
    local port="${SPF_PORT:-8500}"
    local user="${SPF_USER:-risk}"
    local pass="${SPF_PASS:-risk20020309}"
    local base="http://${host}:${port}"

    # 1) 登录拿 token（HttpOnly cookie，curl 用 -c 存到临时文件）。
    local ck
    ck=$(mktemp)
    local login_code
    login_code=$(curl -s -o /dev/null -w '%{http_code}' -c "$ck" \
        -X POST "$base/api/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"${user}\",\"password\":\"${pass}\"}")
    if [ "$login_code" != "200" ]; then
        echo "[health] 登录失败：HTTP $login_code（请检查前端是否启动 / 凭据是否正确）" >&2
        rm -f "$ck"
        return 1
    fi

    # 2) 带 cookie 调 /api/metrics。
    local body
    body=$(curl -s -b "$ck" "$base/api/metrics")
    rm -f "$ck"
    if [ -z "$body" ]; then
        echo "[health] 无法获取 /api/metrics（空响应）" >&2
        return 1
    fi
    echo "[health] $base/api/metrics =>"
    echo "$body" | python3 -m json.tool 2>/dev/null || echo "$body"
}

cmd_help() {
    cat <<'EOF'
SpaceFin 资源管家 manage.sh 用法:

  manage.sh status                查看容器 + systemd 服务状态 + 内存水位
  manage.sh start <tier>          启动整个 tier（t0 恒驻层 / t1 可降级层，含前置检查）
  manage.sh stop  <tier>          停止整个 tier（stop t1 一键释放约 4.7G）
  manage.sh watch [seconds]       每 N 秒(默认30)监控内存，available<2.5G 时告警
  manage.sh health                探测前端 /api/metrics 健康度（需前端已启动+登录态 token）
  manage.sh help                  显示本帮助

Tier 组件清单:
  t0 恒驻  : 容器 spacefin-mysql spacefin-redis
             服务 spacefin-cdc spacefin-cdc-consumer airflow-scheduler airflow-webserver spacefin-frontend
  t1 可降级: 容器 spacefin-doris-fe spacefin-doris-be spacefin-minio
             （精简版已移除 spacefin-kafka / flink-* / spacefin-stream-producer 实时层）
  t2 按需  : 爬虫/离线渲染等任务型组件，用完即弃，本脚本不管理

示例:
  ./manage.sh status
  ./manage.sh stop t1     # 内存吃紧时一键降级，释放 ~4.7G
  ./manage.sh start t1    # 需要跑批/实时链路时恢复
  ./manage.sh watch 30    # 挂监控，自动告警
EOF
}

cmd="${1:-help}"
shift || true
case "$cmd" in
    status) cmd_status ;;
    start|stop)
        tier="${1:-}"
        if [ -z "$tier" ]; then
            echo "用法: $0 $cmd <t0|t1>" >&2
            exit 1
        fi
        tier_action "$cmd" "$tier"
        ;;
    watch) cmd_watch "${1:-30}" ;;
    health) cmd_health ;;
    help|-h|--help) cmd_help ;;
    *)
        echo "未知命令: $cmd" >&2
        cmd_help >&2
        exit 1
        ;;
esac
