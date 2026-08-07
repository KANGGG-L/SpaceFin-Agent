"""train.py 置信度评分：可比案例支撑度（AC-07 精度@覆盖率口径的置信分）。

confidence_score 只用训练集统计（无泄漏）。信号 = 训练集内同 (城市, 小区, 房型)
且面积落在 ±2% / ±5% 区间内的挂牌数：
    score = log1p(cnt±2%) + 0.5·log1p(cnt±5%)
无小区或无可比案例 → 0 分（弃权转人工）。
"""

import numpy as np
import pytest
from avmmods import train

LOG1P_2 = float(np.log1p(2))
LOG1P_3 = float(np.log1p(3))


def _r(city, comm, bed, area):
    return {"city": city, "comm": comm, "bed": bed, "area": area}


def test_no_community_scores_zero_even_if_matches_exist():
    """无小区 → 0 分（弃权转人工），不管训练集里有没有同名案例。"""
    tr = [_r("gz", "天河城", 3, 100.0)]
    rows = [_r("gz", None, 3, 100.0)]
    assert train.confidence_score(rows, {}, {}, tr).tolist() == [0.0]


def test_no_comparable_cases_scores_zero():
    """同 (城市,小区,房型) 但面积落在 ±5% 区间外（或小区不在训练集）→ 无可比 → 0。"""
    tr_far = [_r("gz", "天河城", 3, 50.0)]
    tr_other = [_r("gz", "别的盘", 3, 100.0)]
    rows = [_r("gz", "天河城", 3, 100.0)]
    assert train.confidence_score(rows, {}, {}, tr_far).tolist() == [0.0]
    assert train.confidence_score(rows, {}, {}, tr_other).tolist() == [0.0]


def test_two_percent_band_is_the_main_signal():
    """±2% 严格可比为主信号：cnt±2% 与 cnt±5% 权重 1 : 0.5，边界值计入。"""
    tr = [_r("gz", "天河城", 3, a) for a in (99.0, 100.5, 101.0)]  # 均在 ±2% 内
    rows = [_r("gz", "天河城", 3, 100.0)]
    assert train.confidence_score(rows, {}, {}, tr)[0] == pytest.approx(1.5 * LOG1P_3)

    tr_edge = [_r("gz", "天河城", 3, a) for a in (98.0, 102.0)]  # ±2% 边界值
    rows_edge = [_r("gz", "天河城", 3, 100.0)]
    assert train.confidence_score(rows_edge, {}, {}, tr_edge)[0] == pytest.approx(1.5 * LOG1P_2)


def test_five_percent_only_cases_get_half_weight():
    """±2% 内无案例但 ±5% 内有 → 次级信号 0.5·log1p(cnt±5%)。"""
    tr = [_r("gz", "天河城", 3, a) for a in (96.0, 97.0)]  # 在 [95,105]，不在 [98,102]
    rows = [_r("gz", "天河城", 3, 100.0)]
    assert train.confidence_score(rows, {}, {}, tr)[0] == pytest.approx(0.5 * LOG1P_2)


def test_more_comparable_cases_score_higher():
    tr_1 = [_r("gz", "天河城", 3, 100.0)]
    tr_3 = [_r("gz", "天河城", 3, a) for a in (99.0, 100.0, 101.0)]
    rows = [_r("gz", "天河城", 3, 100.0)]
    s1 = train.confidence_score(rows, {}, {}, tr_1)[0]
    s3 = train.confidence_score(rows, {}, {}, tr_3)[0]
    assert s1 > 0 and s3 > s1


def test_different_city_or_room_type_is_not_comparable():
    tr = [_r("gz", "天河城", 3, 100.0)]
    assert train.confidence_score([_r("sz", "天河城", 3, 100.0)], {}, {}, tr).tolist() == [0.0]
    assert train.confidence_score([_r("gz", "天河城", 2, 100.0)], {}, {}, tr).tolist() == [0.0]


def test_batch_rows_scored_independently():
    tr = [_r("gz", "天河城", 3, a) for a in (99.0, 100.0, 101.0)]
    rows = [
        _r("gz", "天河城", 3, 100.0),  # 可比
        _r("gz", None, 3, 100.0),  # 无小区
        _r("gz", "天河城", 3, 200.0),  # 面积差一倍
    ]
    out = train.confidence_score(rows, {}, {}, tr)
    assert out.shape == (3,)
    assert out[0] > 0
    assert out[1] == 0.0
    assert out[2] == 0.0


def test_confidence_tiers_split_by_comparable_case_count():
    """三档：high=±5% 可比≥10 条，mid=1–9 条，low=无可比（含无小区）。"""
    tr = [_r("gz", "天河城", 3, a) for a in (95.0 + i for i in range(12))]  # ±5% 内 11 条
    rows = [
        _r("gz", "天河城", 3, 100.0),  # 11 条 → high
        _r("gz", "悦泰春天", 3, 100.0),  # 训练集没有 → low
        _r("gz", None, 3, 100.0),  # 无小区 → low
    ]
    tiers = train.confidence_tiers(rows, tr)
    assert tiers["high"]["n"] == 1
    assert tiers["low"]["n"] == 2
    assert tiers["mid"]["n"] == 0
