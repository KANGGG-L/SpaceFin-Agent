"""S4 在架流转率纯函数（turnover.compute_turnover）。

每条用例的目标是「能区分真成功与假成功」，不是「覆盖到代码」：
- 分母为 0 必须 `None` 而不是 `0.0`——`0.0` 会被读成「零流转」的健康信号（陷阱 ⑥）；
- `complete=0` 的 city-day 必须**整天**剔除，既不计下架也不进分母（陷阱 ③）；
- 净版抵消只认「一 removed 一当日新挂」，且不合并主键。

不连库、不读 env：rows 是手造的逐 key 日切片。
"""

import itertools
import math
import os
import sys

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ORCH_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ORCH_DIR)

from turnover import UNKNOWN_BUCKET, compute_turnover, price_bucket  # noqa: E402

DS = "2026-08-31"
OLD = "2026-08-01"
_KEY_SEQ = itertools.count()


def _row(
    status="active",
    city="gz",
    typ="sale",
    first_seen=OLD,
    unit_sig=None,
    price=150,
    complete=1,
):
    return {
        "ds": DS,
        # 重挂不合并主键：每个 key 必须唯一，抵消只发生在计数层面
        "url_key": f"k{next(_KEY_SEQ)}",
        "city": city,
        "typ": typ,
        "status": status,
        "first_seen_date": first_seen,
        "unit_sig": unit_sig,
        "total_price_wan": price,
        "complete": complete,
    }


def _rows(n, **kw):
    return [_row(**kw) for _ in range(n)]


def _only(rows):
    """单组场景：断言只有一组并取出（防止分组维度悄悄变了而用例仍绿）。"""
    out = compute_turnover(rows)
    assert len(out) == 1, f"期望单组，实得 {sorted(out)}"
    return next(iter(out.values()))


# ------------------------------------------------------------------------ 公式
def test_formula_is_hand_calculable():
    """12 在架(t1) = 10 老 + 2 新；3 下架 → t0 = 12-2+3 = 13，分母 = 12.5。"""
    rows = _rows(10) + _rows(2, first_seen=DS) + _rows(3, status="removed")
    m = _only(rows)

    assert (m["active_t1"], m["new"], m["removed"], m["active_t0"]) == (12, 2, 3, 13)
    assert m["denominator"] == 12.5
    assert m["gross"] == pytest.approx(5 / 12.5)
    assert m["removal_rate"] == pytest.approx(3 / 12.5)
    assert m["new_rate"] == pytest.approx(2 / 12.5)
    assert m["net"] == pytest.approx(5 / 12.5)  # 无重挂 → 净 == 毛


# ------------------------------------------------------------ 分母为 0 → None
def test_no_eligible_keys_returns_none_not_zero():
    """陷阱 ⑥：分母为 0 返回 None。返回 0.0 会被读成「零流转」，是假绿。"""
    m = _only(_rows(4, status="unknown") + _rows(2, status="absent"))
    assert m["keys"] == 6
    assert m["active_t1"] == 0 and m["denominator"] == 0
    for k in ("gross", "net", "removal_rate", "new_rate"):
        assert m[k] is None, f"{k} 应为 None，实为 {m[k]!r}"


def test_zero_numerator_with_live_pool_is_zero_not_none():
    """与上面那条的区别：池子非空、当天确实零流转 → 0.0 才是正确且可对外展示的答案。"""
    m = _only(_rows(5))
    assert m["denominator"] == 5.0
    assert m["gross"] == 0.0 and m["net"] == 0.0
    assert m["gross"] is not None


# ------------------------------------------------- 净版：抵消重挂、不合并主键
def test_net_cancels_relist_by_unit_sig():
    """一套下架后重挂：新 ID 的当日新挂 + 老 ID 的 removed，同 sig → 抵消一对。"""
    rows = _rows(4) + _rows(1, status="removed", unit_sig="S1")
    rows += _rows(1, first_seen=DS, unit_sig="S1")
    m = _only(rows)

    assert m["cancelled"] == 1
    assert m["gross"] == pytest.approx(2 / 5)
    assert m["net"] == pytest.approx(0.0)
    assert m["net_removal_rate"] == 0.0 and m["net_new_rate"] == 0.0


def test_relist_is_cancelled_only_against_newly_listed_keys():
    """长期在架的同户型房源不是重挂：抵消只配「当日新挂」，不能乱配对。"""
    rows = _rows(3, unit_sig="S1") + _rows(1, status="removed", unit_sig="S1")
    m = _only(rows)
    assert m["cancelled"] == 0
    assert m["net"] == m["gross"]


def test_unit_sig_none_only_counts_in_gross():
    """没有指纹就无法判断是不是重挂 → 只进毛版，不参与净版抵消。"""
    rows = _rows(2, status="removed", unit_sig=None)
    rows += _rows(2, first_seen=DS, unit_sig=None)
    m = _only(rows)
    assert m["cancelled"] == 0 and m["net"] == m["gross"]


def test_cancellation_does_not_cross_groups():
    """同一 sig 落在不同 (城,段) 是两套不同的房：不得跨组抵消。"""
    rows = _rows(1, status="removed", unit_sig="S1", city="gz")
    rows += _rows(1, first_seen=DS, unit_sig="S1", city="sz")
    rows += _rows(1, status="removed", unit_sig="S2", city="gz", price=80)
    rows += _rows(1, first_seen=DS, unit_sig="S2", city="gz", price=80)
    out = compute_turnover(rows)

    assert out[("gz", "sale", "100-200")]["cancelled"] == 0
    assert out[("sz", "sale", "100-200")]["cancelled"] == 0
    assert out[("gz", "sale", "50-100")]["cancelled"] == 1


def test_gross_is_always_ge_net():
    """不变量：抵消只会减少分子，分母相同 → 毛 ≥ 净（分母为 0 时两边同为 None）。"""
    rows = (
        _rows(6, unit_sig="A")
        + _rows(2, status="removed", unit_sig="A")
        + _rows(2, first_seen=DS, unit_sig="A")
        + _rows(1, status="removed", unit_sig="B")
        + _rows(3, first_seen=DS, unit_sig="C")
        + _rows(2, city="sz", unit_sig="A")
        + _rows(5, city="sz", status="unknown", unit_sig="D")
    )
    for key, m in compute_turnover(rows).items():
        if m["gross"] is None:
            assert m["net"] is None, key
        else:
            assert m["gross"] >= m["net"] >= 0, key


# ------------------------------------------- complete=0 的 city-day 整天剔除
def test_incomplete_city_day_is_dropped_whole_day():
    """陷阱 ③：不完备的 city-day 既不计下架也不进分母——「没看」不是「没了」。"""
    good = _rows(5, city="gz") + _rows(1, city="gz", status="removed")
    bad = _rows(5, city="sz", complete=0) + _rows(2, city="sz", status="removed", complete=0)
    out = compute_turnover(good + bad)

    gz = out[("gz", "sale", "100-200")]
    sz = out[("sz", "sale", "100-200")]
    # sz 行确实被读到了（keys 保留，便于对账），但一行都不进分子分母
    assert sz["keys"] == 7 and sz["complete"] == 0
    assert (sz["active_t1"], sz["removed"], sz["new"], sz["denominator"]) == (0, 0, 0, 0)
    assert sz["gross"] is None and sz["net"] is None
    # 邻近的合格组不受影响
    assert gz["active_t1"] == 5 and gz["gross"] == pytest.approx(1 / 5.5)


def test_one_incomplete_row_invalidates_the_whole_group():
    """组内 complete 标记不一致（对账异常）时按不合格处理：保守方向，不进任何计数。"""
    rows = _rows(4, city="gz") + _rows(1, city="gz", complete=0)
    m = _only(rows)
    assert m["complete"] == 0 and m["keys"] == 5 and m["gross"] is None


def test_only_explicit_complete_one_counts():
    """合格必须显式声明：complete=1 才计入；写 0 与不写是两种「不合格」，都必须剔除。"""
    rows = _rows(4) + _rows(1, status="removed")
    assert _only(rows)["gross"] == pytest.approx(1 / 4.5)  # complete=1（_row 默认）

    for r in rows:
        r["complete"] = 0
    m = _only(rows)
    assert m["complete"] == 0 and m["gross"] is None


def test_missing_complete_flag_drops_the_group_by_default():
    """缺省即未知：行上缺 `complete` 字段 = 不知道合不合格 = 不合格，整组剔除。

    乐观缺省（缺字段当合格）会让不合格的 city-day 静默进分母，算出一个看起来合理、
    实际失真的流转率且无任何报错——这是本项目最典型的假绿形态。宁可返回 None。
    """
    rows = _rows(4) + _rows(1, status="removed")
    for r in rows:
        r.pop("complete")
    m = _only(rows)
    assert m["complete"] == 0 and m["keys"] == 5
    assert m["active_t1"] == 0 and m["denominator"] == 0
    assert m["gross"] is None and m["net"] is None


def test_absent_is_counted_but_enters_no_rate():
    """absent 既不在架也没下架：不进分子分母，但计数必须外露（偏差方向要看得见）。"""
    m = _only(_rows(5) + _rows(3, status="absent") + _rows(2, status="unknown"))
    assert m["absent"] == 3
    assert m["active_t1"] == 5 and m["denominator"] == 5.0
    assert m["gross"] == 0.0


def test_excluded_group_hides_absent_too():
    """整天剔除时 absent 也归零：不合格的 city-day 不提供任何证据。"""
    m = _only(_rows(2, status="absent", complete=0))
    assert m["keys"] == 2 and m["absent"] == 0 and m["gross"] is None


# ------------------------------------------------------------------ 价格分桶
def test_price_buckets_are_a_partition():
    """不重不漏：每个有限非负价格恰好落一个桶，且桶区间包含该值。"""
    assert price_bucket(0) == "0-50"
    assert price_bucket(49.99) == "0-50"
    assert price_bucket(50) == "50-100"  # 半开区间 [lo, hi)
    assert price_bucket(199.99) == "100-200"
    assert price_bucket(200) == "200-300"
    assert price_bucket(999.9) == "500-1000"
    assert price_bucket(1000) == "1000+"
    assert price_bucket(10**9) == "1000+"
    # 无价格证据 → 单列一桶，不混进任何价格段
    for bad in (None, "x", -1, math.nan):
        assert price_bucket(bad) == UNKNOWN_BUCKET


def test_every_key_lands_in_exactly_one_bucket():
    rows = [_row(price=p) for p in (30, 80, 150, 250, 400, 800, 5000, None)]
    rows += [_row(price=150, city="sz"), _row(price=150, typ="fangyuan")]
    out = compute_turnover(rows)
    assert sum(m["keys"] for m in out.values()) == len(rows)
    assert ("gz", "sale", UNKNOWN_BUCKET) in out


def test_explicit_price_bucket_wins_over_total_price():
    row = _row(price=150)
    row["price_bucket"] = "200-300"
    assert ("gz", "sale", "200-300") in compute_turnover([row])
