"""三出口口径一致性校验（AC-05 / TC-08）：validate_consistency 的阻断判定。

三个出口：
  ① internal —— ads_risk_class（风险引擎写的汇总）
  ② dws      —— dws_risk_class 明细 SQL 聚合（独立复算，充当裁判）
  ③ 1104 模板 —— build_g11(internal) 的产物

设计意图是「明细聚合当裁判，拦住汇总表静默漂移」。三个曾经拦不住的口径盲区
（占比同源自比恒等、占比之和=1 无人查、非五级档位静默丢弃）已全部补齐：
  占比不信任 internal 存的 balance_pct，改与 dws 按余额重算的占比比对；
  五级占比之和必须约等于 1；任一读取侧出现非五级档位直接阻断。
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


def test_tampered_template_pct_is_caught_by_dws_recomputed_pct(internal):
    """模板占比来自 internal，被改动后与 dws 按余额重算的占比不符，同样会被拦下。"""
    rows = g11.build_g11(internal)
    next(r for r in rows if r["risk_class"] == "正常")["balance_pct"] = 0.9

    m = g11.validate_consistency(rows, internal, g11mods.dws_of(internal))

    assert "1104_vs_dws:正常:balance_pct" in m


def test_total_row_excluded_from_per_class_validation(internal):
    """校验只遍历五级；合计行是派生值，重复校验没有信息量。"""
    m = validate(internal, g11mods.dws_of(internal))

    assert not any("合计" in x for x in m)


# ================================================================ 补齐的三处口径盲区


def test_wrong_internal_pct_is_caught_against_dws_recomputed_pct(internal):
    """占比不再同源自比：dws 按余额重算占比当裁判，internal 占比写错必被抓。

    旧行为：模板占比从 internal 搬来，「1104 vs internal」的占比比对恒等成立，
    ads_risk_class.balance_pct 写错（如增量刷新时分母用了本批而非全量）会一路
    畅通报送出去——正是模块 docstring 宣称要拦的「汇总错了但没人发现」。
    """
    internal["正常"]["balance_pct"] = 0.99  # 与余额 500/1000 完全不符
    internal["关注"]["balance_pct"] = 0.99  # 各档占比之和远超 1

    m = validate(internal, g11mods.dws_of(internal))

    assert "1104_vs_dws:正常:balance_pct" in m
    assert "1104_vs_dws:关注:balance_pct" in m


def test_pct_not_summing_to_one_blocks(internal):
    """五级占比之和必须约等于 1（EPS_PCT 容差），超过即阻断报送。"""
    for cls in config.CLASS_ORDER:
        internal[cls]["balance_pct"] = 0.0

    m = validate(internal, g11mods.dws_of(internal))

    assert "g11:占比之和:balance_pct" in m


def test_pct_sum_within_tolerance_passes(internal):
    """占比之和偏差远小于容差（DECIMAL(8,4) 舍入级）不阻断。"""
    internal["正常"]["balance_pct"] = 0.5 + g11.EPS_PCT / 10

    assert validate(internal, g11mods.dws_of(internal)) == []


def test_pct_sum_over_tolerance_blocks(internal):
    internal["正常"]["balance_pct"] = 0.5 + g11.EPS_PCT * 10

    m = validate(internal, g11mods.dws_of(internal))

    assert "g11:占比之和:balance_pct" in m


def test_non_five_level_class_in_detail_blocks(conn):
    """非五级档位在 dws 明细聚合时直接阻断，不再静默丢弃。

    旧行为：dws_risk_class 里出现非五级 risk_class（NULL、历史遗留、新增档位）时
    整档余额被丢掉，三出口比对在「都不含该档」的前提下达成一致，报送总额少算
    却不告警。
    """
    conn.queue_result([("正常", 3, 500.0), ("疑似", 5, 5000.0)])

    with pytest.raises(ValueError, match="疑似"):
        g11.load_dws_agg(conn)


def test_total_balance_mismatch_between_paths_blocks(internal):
    """两路径余额总额必须一致：internal 与 dws 的 balance 总和差超容差即阻断。"""
    dws = g11mods.dws_of(internal, 损失=(0, 7.0))

    m = validate(internal, dws)

    assert "dws_vs_internal:总额:balance" in m


def test_all_zero_batch_skips_pct_sum_check():
    """余额为 0 时占比无分母（全 0），占比之和=1 的恒等式不适用，不得误阻断。"""
    empty = g11mods.internal_of()

    assert validate(empty, g11mods.dws_of(empty)) == []


# ================================================================ 演练钩子


def test_simulated_one_yuan_drift_triggers_block(internal):
    """--simulate-mismatch 给首档余额 +1 元，用来验收阻断链路是否真的通。

    1 元远大于 0.01 的容差，必须稳定触发，否则演练是假的。
    """
    first = config.CLASS_ORDER[0]
    dws = g11mods.dws_of(
        internal, **{first: (internal[first]["count"], internal[first]["balance"] + 1.0)}
    )

    m = validate(internal, dws)

    assert f"1104_vs_dws:{first}:balance" in m
    assert f"dws_vs_internal:{first}:balance" in m


@pytest.mark.parametrize("cls", ["正常", "关注", "次级", "可疑", "损失"])
def test_drift_in_any_class_is_blocked(cls, internal):
    """逐档兜一遍，防止校验循环里漏掉某一级。"""
    dws = g11mods.dws_of(
        internal, **{cls: (internal[cls]["count"], internal[cls]["balance"] + 100.0)}
    )

    assert validate(internal, dws) != []
