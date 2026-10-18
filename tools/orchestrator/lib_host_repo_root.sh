#!/bin/bash
# 解析并导出 dockerd 视角的仓库根 SPACEFIN_REPO_ROOT（供 docker-compose.yml 的 bind 源插值）。
#
# 为什么需要它：compose 文件里的 bind 源路径由 **dockerd 在它自己的挂载命名空间**解析，
# 不是由 compose 客户端解析。本仓库的 start_all.sh / stop_stack.sh 常由 Airflow 在
# airflow 容器内调起，届时仓库根是容器路径 /opt/spacefin，相对写法 `../../output/guangdong`
# 会被 dockerd 当成宿主上的绝对路径并**自动新建目录**，于是采集结果写进
# /opt/spacefin/output/guangdong，而 ETL 读的是仓库里的 output/guangdong —— 每晚爬到
# 的新数据静默丢失（2026-08-30 实测，见 docs/tech/components/airflow.md 坑位 #14）。
#
# 取值优先级：调用方显式导出（绝对路径）> 容器内用 findmnt 反查本仓库 bind 的宿主原始
# 路径 > 非容器环境直接用 $PWD。三者都拿不到就硬失败 —— 宁可停住也不要把数据写进
# 另一棵树，静默正是这个坑存活到 2026-08-30 的原因。
spacefin_export_host_repo_root() {
  local repo_root="$1" cur="${SPACEFIN_REPO_ROOT:-}" derived
  # 必须是绝对路径：相对值或空值都会让 bind 源退回 dockerd 视角的猜测
  case "$cur" in
    /*) ;;
    *) cur="" ;;
  esac
  # 容器内把该变量写成容器路径（Airflow Variable spacefin_repo_root 恰好就是这个值）
  # 会原样复现上面的 bug，因此与 repo_root 相等时视为无效。
  if [ -n "$cur" ] && [ "$cur" = "$repo_root" ] && [ -f /.dockerenv ]; then
    cur=""
  fi
  if [ -z "$cur" ] && [ -f /.dockerenv ]; then
    # findmnt 的 SOURCE 形如 /dev/sdd[/home/<user>/GitHub/<repo>]，
    # 方括号内就是该 bind 在宿主上的原始路径。
    derived="$(findmnt -T "$repo_root" -no SOURCE 2>/dev/null | sed -n 's/.*\[\(.*\)\]/\1/p')"
    case "$derived" in
      /*) cur="$derived" ;;
    esac
  fi
  if [ -z "$cur" ] && [ ! -f /.dockerenv ]; then
    cur="$repo_root"
  fi
  if [ -z "$cur" ]; then
    echo "[spacefin] ERROR: 无法确定 dockerd 可见的仓库根（容器内路径 $repo_root 不能用作 bind 源）。" >&2
    echo "[spacefin]        请显式导出宿主绝对路径后重试：export SPACEFIN_REPO_ROOT=/abs/path/to/<repo-root>" >&2
    echo "[spacefin]        排查：findmnt -T $repo_root -no SOURCE  /  docker inspect <airflow 容器> 看 /opt/spacefin 的 Source" >&2
    return 1
  fi
  export SPACEFIN_REPO_ROOT="$cur"
  echo "[spacefin] bind 源使用 dockerd 视角仓库根: $SPACEFIN_REPO_ROOT"
}
