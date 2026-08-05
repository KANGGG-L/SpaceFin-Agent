"""1104 G11 模板生成与出口读取（tools/reporting/main.py）。

G11 是送监管的报表，模板行序、合计行、缺级补零都是硬性格式要求：
报送口径错一位不是「bug」，是合规事故。
"""

import g11mods
import pytest
from g11mods import config, g11

# ================================================================ 出口①：ads_risk_class


def test_load_internal_fills_missing_classes_with_zero(conn):
    """ads_risk_class 里没有「损失」行时也必须凑齐五级，否则模板缺行。"""
    conn.queue_result([("正常", 3, 500.0, 0.5)])

    out = g11.load_internal(conn, "2026-08-05")

    assert list(out) == config.CLASS_ORDER
    assert out["正常"] == {"count": 3, "balance": 500.0, "balance_pct": 0.5}
    assert out["损失"] == {"count": 0, "balance": 0.0, "balance_pct": 0.0}


def test_load_internal_filters_by_business_date(conn):
    conn.queue_result([])

    g11.load_internal(conn, "2026-08-05")

    sql, params = conn.executed[0]
    assert "WHERE stat_date=%s" in sql
    assert params == ("2026-08-05",)


# ================================================================ 出口②：dws 明细聚合


def test_load_dws_agg_has_no_date_filter(conn):
    """DWS 是当前快照表（无 stat_date 分区），按日期过滤会永远聚合出空表。"""
    conn.queue_result([("正常", 3, 500.0)])

    out = g11.load_dws_agg(conn)

    assert "stat_date" not in conn.executed[0][0]
    assert out["正常"] == {"count": 3, "balance": 500.0, "balance_pct": 1.0}
    assert out["损失"] == {"count": 0, "balance": 0.0, "balance_pct": 0.0}


def test_load_dws_agg_recomputes_balance_pct_from_balance(conn):
    """占比按余额重算（分母=全量余额），不信任上游存的 pct——占比校验的独立基准。"""
    conn.queue_result(
        [("正常", 3, 500.0), ("关注", 2, 250.0), ("次级", 1, 150.0), ("可疑", 1, 100.0)]
    )

    out = g11.load_dws_agg(conn)

    assert out["正常"]["balance_pct"] == 0.5
    assert out["次级"]["balance_pct"] == 0.15
    assert out["损失"]["balance_pct"] == 0.0
    assert round(sum(v["balance_pct"] for v in out.values()), 4) == 1.0


def test_load_dws_agg_blocks_on_non_five_level_class(conn):
    """非五级档位（NULL/历史遗留/新增）静默丢弃会让五级总额少算，必须阻断。"""
    conn.queue_result([("正常", 3, 500.0), ("疑似", 5, 5000.0)])

    with pytest.raises(ValueError, match="疑似"):
        g11.load_dws_agg(conn)


def test_load_internal_blocks_on_non_five_level_class(conn):
    """ads_risk_class 同样可能出现非五级档位（store 刷新时不筛），一视同仁阻断。"""
    conn.queue_result([("正常", 3, 500.0, 0.5), ("疑似", 5, 5000.0, 0.9)])

    with pytest.raises(ValueError, match="疑似"):
        g11.load_internal(conn, "2026-08-05")


# ================================================================ G11 模板


def test_template_orders_five_classes_then_total_row(internal):
    rows = g11.build_g11(internal)

    assert [r["risk_class"] for r in rows] == [*config.CLASS_ORDER, "合计"]
    assert [r["is_total"] for r in rows] == [0, 0, 0, 0, 0, 1]


def test_total_row_sums_count_and_balance(internal):
    rows = g11.build_g11(internal)
    total = rows[-1]

    assert total["loan_count"] == 7
    assert total["balance"] == 1000.0
    assert total["balance_pct"] == 1.0


def test_total_row_pct_is_zero_when_balance_is_zero():
    """全部结清是合法状态，不能崩在 total/total 上。"""
    rows = g11.build_g11(g11mods.internal_of())

    assert rows[-1]["balance"] == 0.0
    assert rows[-1]["balance_pct"] == 0.0


def test_template_copies_pct_from_internal_without_recomputing(internal):
    """模板不自己算占比，直接搬 ads_risk_class 的值——这也正是它无法独立校验占比的原因。"""
    internal["正常"]["balance_pct"] = 0.9999

    rows = g11.build_g11(internal)

    assert rows[0]["balance_pct"] == 0.9999


def test_template_rounds_balance_to_cents(internal):
    internal["正常"]["balance"] = 500.014
    internal["关注"]["balance"] = 250.016

    rows = g11.build_g11(internal)

    assert rows[0]["balance"] == 500.01
    assert rows[1]["balance"] == 250.02


def test_float_rounding_deviates_from_financial_expectation_at_half_cent(internal):
    """⚠️ 已知精度问题：金额链路全程 float + round()，不是 Decimal。

    500.005 在二进制里略小于 500.005，round(...,2) 得 500.0 而非财务预期的 500.01。
    余额来自 DECIMAL(16,2) 字段，被 float() 转换后就已丢失精确十进制语义。
    当前量级下影响仅限「分」，但报送口径上这是有说法的，钉住行为备查。
    """
    internal["正常"]["balance"] = 500.005

    rows = g11.build_g11(internal)

    assert rows[0]["balance"] == 500.0


def test_total_balance_is_summed_before_rounding(internal):
    """合计是对原始值求和后再舍入，不是对已舍入的各档求和。"""
    internal["正常"]["balance"] = 0.004
    internal["关注"]["balance"] = 0.004
    internal["次级"]["balance"] = 0.004
    internal["可疑"]["balance"] = 0.004

    rows = g11.build_g11(internal)

    assert [r["balance"] for r in rows[:4]] == [0.0, 0.0, 0.0, 0.0]
    assert rows[-1]["balance"] == 0.02  # 逐档舍入会得 0.0


# ================================================================ 输出格式


def test_csv_export_renders_pct_as_percentage(tmp_path, internal):
    rows = g11.build_g11(internal)

    csv_path, _ = g11.write_outputs(
        str(tmp_path), "2026-08-05", rows, "submitted", [], {"date": "2026-08-05"}
    )

    lines = open(csv_path, encoding="utf-8").read().strip().splitlines()
    assert lines[0] == "risk_class,loan_count,balance,balance_pct"
    assert lines[1] == "正常,3,500.0,50.0"
    assert lines[-1].startswith("合计,7,1000.0,100.0")


def test_json_export_keeps_both_fraction_and_percentage(tmp_path, internal):
    """审计要能复算：分数占比是计算口径，百分数只是展示。"""
    import json

    rows = g11.build_g11(internal)
    _, json_path = g11.write_outputs(
        str(tmp_path), "2026-08-05", rows, "submitted", [], {"date": "2026-08-05"}
    )

    doc = json.load(open(json_path, encoding="utf-8"))
    assert doc["status"] == "submitted"
    assert doc["mismatches"] == []
    assert doc["rows"][0]["balance_pct"] == 0.5
    assert doc["rows"][0]["balance_pct_percent"] == 50.0


def test_blocked_run_still_writes_outputs_with_mismatches(tmp_path, internal):
    """拒报也要留痕：合规要能看到「为什么没报」。"""
    import json

    rows = g11.build_g11(internal)
    _, json_path = g11.write_outputs(
        str(tmp_path), "2026-08-05", rows, "blocked", ["1104_vs_dws:正常:balance"], {}
    )

    doc = json.load(open(json_path, encoding="utf-8"))
    assert doc["status"] == "blocked"
    assert doc["mismatches"] == ["1104_vs_dws:正常:balance"]


def test_output_filenames_include_business_date(tmp_path, internal):
    """跨日重跑各留各的文件，不能互相覆盖。"""
    rows = g11.build_g11(internal)

    csv_path, json_path = g11.write_outputs(str(tmp_path), "2026-08-05", rows, "submitted", [], {})

    assert csv_path.endswith("ads_1104_g11_2026-08-05.csv")
    assert json_path.endswith("g11_report_2026-08-05.json")


# ================================================================ 落库


def test_g11_upsert_is_idempotent_per_date_and_class(conn, internal):
    """同一 (stat_date, risk_class) 重跑覆盖，不产生脏数据。"""
    rows = g11.build_g11(internal)

    g11.upsert_g11(conn, "2026-08-05", rows)

    sql, sent = conn.find_many("INSERT INTO ads_1104_g11")
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert len(sent) == 6  # 五级 + 合计
    assert sent[0][0] == "2026-08-05"
    assert conn.commits == 1


def test_total_row_persisted_with_is_total_flag(conn, internal):
    """报送模板含合计行；下游筛 is_total=0 才是五级明细，标记丢了会重复计数。"""
    rows = g11.build_g11(internal)

    g11.upsert_g11(conn, "2026-08-05", rows)

    _, sent = conn.find_many("INSERT INTO ads_1104_g11")
    assert sent[-1][1] == "合计"
    assert sent[-1][5] == 1
    assert all(s[5] == 0 for s in sent[:5])


def test_each_mismatch_recorded_as_blocking_alert(conn):
    conn_rows = ["1104_vs_dws:正常:balance", "dws_vs_internal:可疑:loan_count"]

    g11.write_alerts(conn, "2026-08-05", conn_rows)

    sql, sent = conn.find_many("INSERT INTO ads_report_alert")
    assert len(sent) == 2
    assert {s[3] for s in sent} == set(conn_rows)
    assert all(s[2] == "block" for s in sent)  # 级别是阻断，不是提示
    assert all(s[1] == g11.REPORT_TYPE for s in sent)


def test_reporting_reuses_risk_class_order():
    """CLASS_ORDER 直接复用 tools/risk/config，避免报送侧与风险侧档位漂移。"""
    assert g11.CLASS_ORDER == config.CLASS_ORDER
    assert g11.REPORT_TYPE == "1104_g11"


@pytest.mark.parametrize(("eps", "expected"), [("EPS_BALANCE", 0.01), ("EPS_PCT", 0.0001)])
def test_tolerances_match_column_precision(eps, expected):
    """余额是 DECIMAL(16,2)、占比是 DECIMAL(8,4)，容差必须与库里精度对齐。"""
    assert getattr(g11, eps) == expected
