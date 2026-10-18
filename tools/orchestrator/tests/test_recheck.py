"""`recheck`：定向复核的目标选取（计划 S5 · 纯函数 `select_recheck_targets`）。

核心不变式有三条，每条都是防「假绿」的一道闸门：

1. **unknown 绝不进复核集** —— unknown 是「没扫到」，不是「不在了」；把它拿去详情页直连，
   等于用爬虫的洞冒充房源的状态。用例里 unknown 即便 streak 正好是 1 也必须落选。
2. **配额是硬上限** —— 定向复核的预算远小于全量回扫，超限必须截断且**留痕**；
   静默截断会让运维以为覆盖了全部候选。
3. **确定性** —— 打乱输入顺序输出必须逐项相同，否则复核集不可复现、无法与前日做差。

载入方式照 test_market_state.py / test_census.py：用 importlib 按文件路径加载，
避免 `pytest tools/` 联跑时多个 tests 目录的 conftest 串味。
"""

import importlib.util
import os
import random
import sys

import pytest

ORCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location(
    "recheck_under_test", os.path.join(ORCH_DIR, "recheck.py")
)
rc = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rc
SPEC.loader.exec_module(rc)


ABSENT = "absent"
UNKNOWN = "unknown"
ACTIVE = "active"
REMOVED = "removed"


def row(city, typ, streak, idx=0, status=ABSENT):
    """url_key 里带上城/型/序号，便于直接断言「哪个组的哪些 key 中选」。"""
    return {
        "url_key": f"{city}:{typ}:s{streak}:{idx:03d}",
        "city": city,
        "typ": typ,
        "absent_streak": streak,
        "market_status": status,
    }


def picks(city, typ, streak, count, status=ABSENT):
    return [row(city, typ, streak, i, status) for i in range(count)]


def test_only_streak_1_and_n_minus_1_are_selected():
    rows = [
        row("gz", "sale", 0),
        row("gz", "sale", 1),
        row("gz", "sale", 2),
        row("gz", "sale", 3),
        row("gz", "sale", 9),
    ]
    keys, stats = rc.select_recheck_targets(rows, 3)
    # streak 降序：N-1 那一档排在前面（见 test_streak_n_minus_1_outranks_streak_1）
    assert keys == ["gz:sale:s2:000", "gz:sale:s1:000"]
    assert stats["total_candidates"] == 2


def test_streak_at_or_above_n_is_never_selected():
    """streak >= N 已经判成 removed 了，不再是「待确认」。"""
    rows = picks("gz", "sale", 3, 5) + picks("gz", "sale", 4, 5) + picks("gz", "sale", 2, 1)
    keys, _ = rc.select_recheck_targets(rows, 3)
    assert keys == ["gz:sale:s2:000"]


@pytest.mark.parametrize("status", [UNKNOWN, ACTIVE, REMOVED])
def test_non_absent_status_is_never_selected(status):
    """unknown 即使 streak 命中白名单也不复核：它要先解决「为什么没扫到」。"""
    rows = [row("gz", "sale", 1, 0, status), row("gz", "sale", 2, 1, status)]
    keys, stats = rc.select_recheck_targets(rows, 3)
    assert keys == []
    assert stats["total_candidates"] == 0


def test_per_group_quota_truncates_and_is_marked():
    """同组超限截断，且该组必须出现在 truncated_groups 里。"""
    gz = picks("gz", "sale", 1, 7)
    zs = picks("zs", "sale", 1, 2)
    keys, stats = rc.select_recheck_targets(gz + zs, 3, quota_per_group=3)
    assert len(keys) == 5  # gz 截到 3 个 + zs 全选 2 个
    assert stats["truncated_groups"] == [["gz", "sale"]]
    assert not stats["total_truncated"]
    assert keys[:3] == [r["url_key"] for r in gz[:3]]  # 截断保留排序在前的
    assert stats["total_candidates"] == 9


def test_per_group_quota_counts_streaks_together():
    """配额按 (city, typ) 组算，同一组的两个 streak 档共用一份配额。"""
    rows = picks("gz", "sale", 1, 4) + picks("gz", "sale", 2, 4)
    keys, stats = rc.select_recheck_targets(rows, 3, quota_per_group=5)
    assert len(keys) == 5
    assert stats["truncated_groups"] == [["gz", "sale"]]
    # 降序下 streak=N-1 先占满名额，挤掉部分 streak=1 —— 见模块 docstring「为什么降序」
    assert keys[:4] == [r["url_key"] for r in picks("gz", "sale", 2, 4)]
    assert keys[4] == "gz:sale:s1:000"


def test_streak_n_minus_1_outranks_streak_1_when_quota_bites():
    """钉死优先级：配额只够选一半时，入选的必须全是 streak=N-1，streak=1 一个都不进。

    streak=N-1 是下架判定的最后一道关，误判会直接造出一个错误的「下架」事件并污染
    流转率分子；streak=1 还有 N-1 天缓冲、后续轮次还会再进候选。所以截断必须先砍
    streak=1 —— 这条用例防止有人把排序改回升序（改回去的过程是静默的）。
    """
    rows = picks("gz", "sale", 1, 10) + picks("gz", "sale", 2, 10)
    keys, stats = rc.select_recheck_targets(rows, 3, quota_per_group=10)
    assert len(keys) == 10
    assert stats["total_candidates"] == 20
    assert stats["truncated_groups"] == [["gz", "sale"]]
    assert keys == [r["url_key"] for r in picks("gz", "sale", 2, 10)]
    assert not any(":s1:" in k for k in keys)


def test_total_quota_truncates_and_flags_without_marking_groups():
    """总量截断：排在后面的组一个名额都拿不到，那不是 per-group 截断。"""
    rows = picks("gz", "sale", 1, 10) + picks("zs", "sale", 1, 10) + picks("dg", "sale", 1, 10)
    keys, stats = rc.select_recheck_targets(rows, 3, quota_per_group=5, quota_total=7)
    assert len(keys) == 7
    assert stats["total_truncated"] is True
    # 升序是 dg < gz < zs：dg 吃满 5 个、gz 拿到 2 个后总量断供，zs 一个都没有
    assert keys == [r["url_key"] for r in picks("dg", "sale", 1, 5)] + [
        r["url_key"] for r in picks("gz", "sale", 1, 2)
    ]
    # 只有 dg 是「被 per-group 截断」；gz/zs 是总量断供，不该混进 truncated_groups
    assert stats["truncated_groups"] == [["dg", "sale"]]
    assert stats["total_candidates"] == 30


def test_zero_quota_selects_nothing():
    rows = picks("gz", "sale", 1, 5)
    assert rc.select_recheck_targets(rows, 3, quota_per_group=0)[0] == []
    assert rc.select_recheck_targets(rows, 3, quota_total=0)[0] == []


@pytest.mark.parametrize("n", [1, 2])
def test_n_le_2_collapses_whitelist_to_streak_1(n):
    """n<=2 时 {1, n-1} 去重只剩 1：不报错、不选空。"""
    rows = picks("gz", "sale", 1, 3) + picks("gz", "sale", 2, 3)
    keys, stats = rc.select_recheck_targets(rows, n)
    assert keys == [r["url_key"] for r in picks("gz", "sale", 1, 3)]
    assert stats["streaks"] == [1]


def test_n_1_never_selects_streak_zero():
    """n=1 时 n-1=0，但 absent 且 streak=0 自相矛盾，必须剔除。"""
    rows = [row("gz", "sale", 0), row("gz", "sale", 1)]
    keys, stats = rc.select_recheck_targets(rows, 1)
    assert keys == ["gz:sale:s1:000"]
    assert stats["streaks"] == [1]


def test_empty_and_no_match_return_empty_without_error():
    for rows in ([], picks("gz", "sale", 5, 4), [row("gz", "sale", 1, 0, UNKNOWN)]):
        keys, stats = rc.select_recheck_targets(rows, 3)
        assert keys == []
        assert stats["selected"] == 0
        assert stats["truncated_groups"] == []


def test_order_is_deterministic_regardless_of_input_order():
    rows = []
    for city in ("zs", "gz", "dg"):
        for typ in ("fangyuan", "sale"):
            for streak in (2, 1):
                rows.extend(picks(city, typ, streak, 4))
    expected, expected_stats = rc.select_recheck_targets(rows, 3)

    shuffled = list(rows)
    random.Random(20260831).shuffle(shuffled)
    assert rc.select_recheck_targets(shuffled, 3) == (expected, expected_stats)

    # 城/型升序 + streak 降序 + url_key 升序
    assert expected == sorted(
        expected,
        key=lambda k: (k.split(":")[0], k.split(":")[1], -int(k.split(":")[2][1:]), k),
    )


def test_invalid_params_raise_loudly():
    rows = picks("gz", "sale", 1, 2)
    with pytest.raises(ValueError):
        rc.select_recheck_targets(rows, 0)  # n < 1
    with pytest.raises(ValueError):
        rc.select_recheck_targets(rows, 3, quota_per_group=-1)
    with pytest.raises(ValueError):
        rc.select_recheck_targets(rows, 3, quota_total=-1)
    with pytest.raises(ValueError):
        rc.select_recheck_targets([row("gz", "sale", "1")], 3)  # streak 非 int
    with pytest.raises(ValueError):
        rc.select_recheck_targets([row("gz", "sale", True)], 3)  # bool 不算 int


def test_stats_counts_stay_consistent_under_heavy_truncation():
    rows = picks("gz", "sale", 1, 100) + picks("zs", "fangyuan", 2, 100)
    keys, stats = rc.select_recheck_targets(rows, 3, quota_per_group=10, quota_total=15)
    assert stats["total_candidates"] == 200
    assert stats["selected"] == len(keys) == 15
    assert stats["truncated_groups"] == [["gz", "sale"]]
    assert stats["total_truncated"] is True
