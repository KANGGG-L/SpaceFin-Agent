#!/usr/bin/env python3
"""定向复核的目标选取（S5 生命周期计划）。

本文件只有**纯函数**：不连库、不读环境变量、不 import etl / census / market_state，
单测直接喂 `list[dict]` 就能跑完整条选取链路。详情页抓取与 CLI 暂不实现 ——
它们要等影子期跑满 13 天、S4 净版上线后才有输入。

为什么只挑 `market_status == 'absent'` 且 `absent_streak ∈ {1, N-1}` 的 key：

  unknown 不复核   unknown 的语义就是「今天没扫到，不等于下架」。它要修的是观测侧
                   （账本没有 complete=1、页码不在认证区间内），拿去详情页直连等于把
                   爬虫的洞当成房源的状态 —— 那正是 S3 花整章去防的假绿。
  streak = 1       刚开始缺席：可能是真下架，也可能只是没扫到，值得确认。
  streak = N-1     再缺席一次就要判下架了，判之前值得确认（价值最高的一档）。
  streak >= N      已走完 `decide_removal` 判成 removed，不再是「待确认」。
  streak = 0       absent 与 streak=0 自相矛盾，一律不选（见 `_streak_whitelist`）。

**配额是硬约束**（`quota_per_group` / `quota_total`）：定向复核的预算远小于全量回扫，
一旦放开就退化成「再扫一遍」，S5 的性价比假设当场失效。所以超限一律**截断**，不扩配额；
截断必须留痕（`truncated_groups` / `total_truncated`）—— 静默截断会让人以为覆盖了全部
候选，这是本项目反复踩过的假绿。

排序确定性：候选按 `(city, typ) 升序 + absent_streak 降序 + url_key 升序` 依次取用，
同输入必得同输出。否则每次跑出的复核集都不一样，既无法复现，也没法和前一天的复核结果做差。

**为什么 `absent_streak` 是降序（N-1 优先），不是升序**（D-? 已定，改前先读懂）：
排序即优先级 —— 配额截断时，排前面的留下、排后面的被丢掉。复核的边际价值不在「数量多」，
而在「拦住代价最高的误判」：

  streak = N-1 是下架判定的**最后一道关**，明天再缺席一次就判 removed。这里的误判会直接
           产生一个错误的「下架」事件，污染流转率的**分子**，而且判成之后要靠
           `prev_removed_date` 一直带下去，越往后越难纠正。
  streak = 1 还有 N-1 天缓冲，这批 key 后续轮次会再次进入候选，机会不止这一次。

所以配额有限时先保住「即将判下架」的那批。这不是随手定的降序 —— 若改回升序，
被挤掉的就是最需要确认的那档，且过程是静默的（只剩 `truncated_groups` 里一个组名）。

非法参数（n < 1、配额为负、streak 不是 int）是调用方 bug，抛 `ValueError` 响亮失败，
绝不静默兜底成 0 或全选 —— 静默兜底在这里的表现就是「悄悄退化成全量回扫」。
"""

from __future__ import annotations

# 与 market_state.STATUS_ABSENT 同值；刻意不 import，保持纯函数层零依赖
STATUS_ABSENT = "absent"


def _require_int(value, field: str, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} 必须是 int，收到 {value!r}")
    if value < minimum:
        raise ValueError(f"{field} 必须 >= {minimum}，收到 {value!r}")
    return value


def _streak_whitelist(n: int) -> list[int]:
    """生效的 streak 白名单：`{1, n-1}` 去重后剔除 0，升序返回。

    `n <= 2` 时 `{1, n-1}` 去重只剩 `{1}` —— 不报错、不选空，仍返回 streak=1 这一档。
    0 必须剔除：「absent 但 streak=0」是自相矛盾的行（当天还在架却被记缺席），
    选它等于把刚在架的房源也拉去复核。`n=1` 时 `n-1=0` 正是这种情况。
    """
    return sorted({1, n - 1} - {0})


def select_recheck_targets(rows, n, quota_per_group=50, quota_total=2000) -> tuple:
    """从当日状态切片里挑出「值得定向复核」的 key → `(选中的 url_key 列表, 统计信息)`。

    参数：
      rows              逐 key 的当日状态切片，元素须含
                        `url_key` / `city` / `typ` / `absent_streak` / `market_status`
      n                 判下架所需的连续缺席天数 N（int >= 1，生产固定 3）
      quota_per_group   每个 (city, typ) 组最多选多少（int >= 0；0 = 该组不选）
      quota_total       本次总共最多选多少（int >= 0；0 = 全不选）

    返回：
      keys   中选的 `url_key` 列表，顺序即选取顺序（确定性，可复现）
      stats  {
               'total_candidates': 命中 status+streak 条件的候选总数（配额前），
               'selected':         实际选中的数量，
               'truncated_groups': 触到 per-group 配额被截断的组 `[[city, typ], ...]`（已排序），
               'total_truncated':  是否触到总量配额（bool），
               'streaks':          实际生效的 streak 白名单（n<=2 时可见去重结果），
             }

    `truncated_groups` 与 `total_truncated` 必须分开看：触到总量配额时，排在后面的组是
    一个名额都没拿到，不是「被 per-group 截断」，所以只置 `total_truncated` 而不进
    `truncated_groups`。两者都不看，就会把「截断了」读成「覆盖全了」。
    """
    _require_int(n, "n", 1)
    _require_int(quota_per_group, "quota_per_group", 0)
    _require_int(quota_total, "quota_total", 0)
    whitelist = _streak_whitelist(n)

    candidates = []
    for row in rows:
        if row["market_status"] != STATUS_ABSENT:
            continue
        streak = _require_int(row["absent_streak"], "absent_streak", 0)
        if streak in whitelist:
            candidates.append(row)

    # 确定性排序 = 优先级：(city, typ) 升序 + streak 降序（N-1 优先，见模块 docstring）
    candidates.sort(key=lambda r: (r["city"], r["typ"], -r["absent_streak"], r["url_key"]))

    selected = []
    used_per_group: dict[tuple, int] = {}
    truncated_groups: set[tuple] = set()
    total_truncated = False
    for row in candidates:
        group = (row["city"], row["typ"])
        used = used_per_group.get(group, 0)
        if used >= quota_per_group:
            truncated_groups.add(group)
            continue
        if len(selected) >= quota_total:
            # 后面的组连一个名额都拿不到，但这不是「被 per-group 截断」，只记总量截断
            total_truncated = True
            break
        used_per_group[group] = used + 1
        selected.append(row)

    stats = {
        "total_candidates": len(candidates),
        "selected": len(selected),
        "truncated_groups": [[c, t] for c, t in sorted(truncated_groups)],
        "total_truncated": total_truncated,
        "streaks": whitelist,
    }
    return [row["url_key"] for row in selected], stats
