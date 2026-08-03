#!/bin/sh
# SpaceFin worker 启动包装（B1 并行化 fangyuan）：
#   每个 worker 启动时，从只读挂载的 chrome_profile_src（宿主机已验证的 58/安居客会话）
#   拷贝一份私有可写副本 profile_${WORKER_ID}，并 export ANJUKE_PROFILE_DIR 指向它。
#   这样 5 个 Chrome 各用各的 user-data-dir，不再争抢同一目录的 SingletonLock。
#   master（无 WORKER_ID）跳过拷贝，直接启动。
set -e

if [ -n "$WORKER_ID" ]; then
    SRC=/app/anjuke_crawler/chrome_profile_src
    DST=/app/anjuke_crawler/profile_${WORKER_ID}
    if [ -d "$SRC" ] && [ -n "$(ls -A "$SRC" 2>/dev/null)" ]; then
        rm -rf "$DST"
        cp -r "$SRC" "$DST"
        # 清掉源里可能残留的 Chrome 锁，避免新实例误判 profile 被占用
        rm -f "$DST/SingletonLock" "$DST/SingletonCookie" 2>/dev/null || true
        export ANJUKE_PROFILE_DIR="$DST"
    fi
fi

exec python "$@"
