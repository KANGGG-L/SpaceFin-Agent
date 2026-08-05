"""三出口口径一致性校验（AC-05 / TC-08）：validate_consistency 的阻断判定。

三个出口：
  ① internal —— ads_risk_class（风险引擎写的汇总）
  ② dws      —— dws_risk_class 明细 SQL 聚合（独立复算，充当裁判）
  ③ 1104 模板 —— build_g11(internal) 的产物

设计意图是「明细聚合当裁判，拦住汇总表静默漂移」。这里既验证它拦得住什么，
也钉住它**拦不住**什么——后者对合规场景更要命。
"""

import g11mods
import pytest
from g11mods import config, g11


def validate(internal, dws):
    return g11.validate_consistency(g11.build_g11(internal), internal, dws)


# ================================================================ 一致即放行


def test_three_sources_in_agreement_yield_no_mismatch(internal):
    assert validate(internal, g11mods.dws_of(internal)) == []


def test_all_zero_batch_is_consistent():
    """当日无贷款也要能正常报送（报一张零表），不能被误判成漂移。"""
    empty = g11mods.internal_of()

    assert validate(empty, g11mods.dws_of(empty)) == []


# ================================================================ 明细与汇总漂移 → 阻断


def test_detail_count_drift_blocks_submission(internal):
    dws = g11mods.dws_of(internal, 正常=(4, 500.0))

    m = validate(internal, dws)

    assert "1104_vs_dws:正常:loan_count" in m
    assert "dws_vs_internal:正常:loan_count" in m


def test_detail_balance_drift_blocks_submission(internal):
    dws = g11mods.dws_of(internal, 可疑=(1, 200.0))

    m = validate(internal, dws)

    assert "1104_vs_dws:可疑:balance" in m
    assert "dws_vs_internal:可疑:balance" in m


def test_each_class_validated_independently(internal):
    """一个档位对上了不能让另一个档位的漂移蒙混过关。"""
    dws = g11mods.dws_of(internal, 正常=(9, 500.0), 损失=(0, 7.0))

    m = validate(internal, dws)

    assert any("正常:loan_count" in x for x in m)
    assert any("损失:balance" in x for x in m)


def test_mismatch_labels_identify_source_class_and_metric(internal):
    """告警要能直接告诉合规「哪一档、哪个指标、哪两个出口对不上」。"""
    m = validate(internal, g11mods.dws_of(internal, 次级=(1, 999.0)))

    assert all(x.count(":") == 2 for x in m)
    assert {x.split(":")[0] for x in m} <= {"1104_vs_internal", "1104_vs_dws", "dws_vs_internal"}


# ================================================================ 容差边界


def test_balance_diff_exactly_at_tolerance_passes(internal):
    """容差是「严格大于才算漂移」：1 分钱的 DECIMAL 舍入差不该拒报。"""
    dws = g11mods.dws_of(internal, 正常=(3, 500.0 + g11.EPS_BALANCE))

    assert validate(internal, dws) == []


def test_balance_diff_just_over_tolerance_blocks(internal):
    dws = g11mods.dws_of(internal, 正常=(3, 500.0 + g11.EPS_BALANCE * 2))

    assert "1104_vs_dws:正常:balance" in validate(internal, dws)


def test_count_has_no_tolerance(internal):
    """笔数是整数，没有舍入问题，差 1 笔就是真漂移。"""
    dws = g11mods.dws_of(internal, 关注=(3, 250.0))

    assert "1104_vs_dws:关注:loan_count" in validate(internal, dws)


# ================================================================ 模板自检


def test_tampered_template_count_is_caught_by_self_check(internal):
    """1104 模板与 internal 同源，比对它俩是自检；模板被改动应立刻暴露。"""
    rows = g11.build_g11(internal)
    next(r for r in rows if r["risk_class"] == "正常")["loan_count"] = 99

    m = g11.validate_consistency(rows, internal, g11mods.dws_of(internal))

    assert "1104_vs_internal:正常:loan_count" in m


def test_tampered_template_pct_is_caught_by_self_check(internal):
    rows = g11.build_g11(internal)
    next(r for r in rows if r["risk_class"] == "正常")["balance_pct"] = 0.9

    m = g11.validate_consistency(rows, internal, g11mods.dws_of(internal))

    assert "1104_vs_internal:正常:balance_pct" in m


def test_total_row_excluded_from_per_class_validation(internal):
    """校验只遍历五级；合计行是派生值，重复校验没有信息量。"""
    m = validate(internal, g11mods.dws_of(internal))

    assert not any("合计" in x for x in m)


# ================================================================ 校验拦不住的场景（口径缺口）


def test_wrong_pct_is_not_caught_by_detail_aggregate(internal):
    """⚠️ 校验缺口：占比只在「1104 模板 vs internal」之间比，而模板的占比就是从

    internal 搬来的——同源自比恒等。出口② 的 SQL 聚合根本不产出 balance_pct，
    没有任何一方独立复算过占比。于是 ads_risk_class.balance_pct 写错（例如增量
    刷新时分母用了本批而非全量）会**一路畅通报送出去**，而这正是模块 docstring
    宣称要拦的「汇总错了但没人发现」。
    """
    internal["正常"]["balance_pct"] = 0.99  # 与余额 500/1000 完全不符
    internal["关注"]["balance_pct"] = 0.99  # 各档占比之和远超 1

    assert validate(internal, g11mods.dws_of(internal)) == []


def test_pct_not_summing_to_one_does_not_block(internal):
    """五级占比之和必须约等于 1 是 G11 的基本恒等式，但当前没有任何一条检查覆盖它。"""
    for cls in config.CLASS_ORDER:
        internal[cls]["balance_pct"] = 0.0

    m = validate(internal, g11mods.dws_of(internal))

    assert m == []
    rows = g11.build_g11(internal)
    assert sum(r["balance_pct"] for r in rows if not r["is_total"]) == 0.0


def test_out_of_vocabulary_class_silently_dropped_from_detail(conn):
    """⚠️ 校验缺口：load_dws_agg 只按 CLASS_ORDER 取值，dws_risk_class 里出现

    非五级的 risk_class（NULL、历史遗留、新增档位）时整档余额被丢掉。
    三出口比对随后在「都不含该档」的前提下达成一致，报送总额少算却不阻断。
    """
    conn.queue_result([("正常", 3, 500.0), ("疑似", 5, 5000.0)])

    dws = g11.load_dws_agg(conn)

    assert "疑似" not in dws
    assert sum(v["balance"] for v in dws.values()) == 500.0  # 5000 消失且无人告警


# ================================================================ 演练钩子


def test_simulated_one_yuan_drift_triggers_block(internal):
    """--simulate-mismatch 给首档余额 +1 元，用来验收阻断链路是否真的通。

    1 元远大于 0.01 的容差，必须稳定触发，否则演练是假的。
    """
    first = config.CLASS_ORDER[0]
    dws = g11mods.dws_of(internal)
    dws[first] = {"count": dws[first]["count"], "balance": dws[first]["balance"] + 1.0}

    m = validate(internal, dws)

    assert f"1104_vs_dws:{first}:balance" in m
    assert f"dws_vs_internal:{first}:balance" in m


@pytest.mark.parametrize("cls", ["正常", "关注", "次级", "可疑", "损失"])
def test_drift_in_any_class_is_blocked(cls, internal):
    """逐档兜一遍，防止校验循环里漏掉某一级。"""
    dws = g11mods.dws_of(internal)
    dws[cls] = {"count": dws[cls]["count"], "balance": dws[cls]["balance"] + 100.0}

    assert validate(internal, dws) != []
