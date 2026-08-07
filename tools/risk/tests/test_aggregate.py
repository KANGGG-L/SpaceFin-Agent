"""五级分类汇总（risk_engine.build_aggregate）：笔数 / 余额 / 占比口径。

这是 ads_risk_class 与 1104 G11 报送的上游，占比算错会一路错到监管报表。
"""

import pytest
from riskmods import config, risk_engine


def row(cls, balance):
    return {"risk_class": cls, "balance": balance}


def test_always_emits_all_five_classes_even_when_empty():
    """缺级补零，否则下游报送模板会缺行、占比分母对不齐。"""
    agg = risk_engine.build_aggregate([row("正常", 100.0)])

    assert list(agg["by_class"]) == config.CLASS_ORDER
    assert agg["by_class"]["损失"] == {"count": 0, "balance": 0.0, "balance_pct": 0.0}


def test_accumulates_count_and_balance_per_class():
    rows = [row("正常", 100.0), row("正常", 300.0), row("可疑", 600.0)]

    agg = risk_engine.build_aggregate(rows)

    assert agg["by_class"]["正常"]["count"] == 2
    assert agg["by_class"]["正常"]["balance"] == 400.0
    assert agg["by_class"]["可疑"]["count"] == 1
    assert agg["total_loans"] == 3
    assert agg["total_balance"] == 1000.0


def test_balance_pct_rounded_to_four_decimals():
    rows = [row("正常", 1.0), row("关注", 2.0)]

    agg = risk_engine.build_aggregate(rows)

    assert agg["by_class"]["正常"]["balance_pct"] == 0.3333
    assert agg["by_class"]["关注"]["balance_pct"] == 0.6667


def test_class_pcts_sum_to_one():
    rows = [row("正常", 250.0), row("关注", 250.0), row("次级", 250.0), row("损失", 250.0)]

    agg = risk_engine.build_aggregate(rows)

    assert sum(v["balance_pct"] for v in agg["by_class"].values()) == pytest.approx(1.0, abs=1e-4)


def test_empty_batch_yields_zero_totals_without_division():
    agg = risk_engine.build_aggregate([])

    assert agg["total_loans"] == 0
    assert agg["total_balance"] == 0.0
    assert all(v["balance_pct"] == 0.0 for v in agg["by_class"].values())


def test_zero_total_balance_yields_zero_pct():
    """全部贷款已结清（余额 0）是合法状态，不能因为分母为 0 崩掉汇总。"""
    agg = risk_engine.build_aggregate([row("正常", 0.0), row("损失", 0.0)])

    assert agg["total_balance"] == 0.0
    assert agg["by_class"]["正常"]["balance_pct"] == 0.0


def test_null_balance_counted_as_zero():
    agg = risk_engine.build_aggregate([row("正常", None), row("正常", 50.0)])

    assert agg["by_class"]["正常"]["count"] == 2
    assert agg["by_class"]["正常"]["balance"] == 50.0


def test_unknown_class_dropped_but_still_counted_in_total():
    """⚠️ 口径漏洞：risk_class 不在五级里（脏数据/新增档位）时余额被静默丢弃，

    而 total_loans 用的是 len(rows)。于是 by_class 的笔数之和 < total_loans，
    total_balance 也少算——不会报错，只会让报表悄悄对不上。这里钉住当前行为。
    """
    agg = risk_engine.build_aggregate([row("正常", 100.0), row("疑似", 900.0)])

    assert agg["total_loans"] == 2
    assert sum(v["count"] for v in agg["by_class"].values()) == 1
    assert agg["total_balance"] == 100.0  # 900 被吞掉
    assert agg["by_class"]["正常"]["balance_pct"] == 1.0  # 分母也随之失真
