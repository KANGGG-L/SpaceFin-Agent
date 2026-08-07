"""读写层（store.py）：全量与 CDC 增量共用的加载 / 打宽 / 落库语义。

这里不连真实 MySQL，用 riskmods.FakeConn 录制 SQL 与参数，断言的是
「发出了什么语义的语句、参数对不对、空输入会不会白跑一趟」。
store.py 的价值主张就是「增量与全量口径一致」，这些语义是它唯一的保障。
"""

import pytest
from riskmods import FakeConn, RaisingConn, config, store

LOAN_COLS = [
    "loan_id",
    "customer_id",
    "collateral_id",
    "loan_amount",
    "balance",
    "interest_rate",
    "risk_class",
    "origination_date",
]


# ================================================================ IN 子句


def test_in_clause_placeholder_count_matches_params():
    frag, params = store._in_clause([7, 8, 9])

    assert frag == "(%s,%s,%s)"
    assert params == [7, 8, 9]


def test_in_clause_never_inlines_values():
    """参数化是 SQL 注入的唯一防线——片段里只能有占位符。"""
    frag, params = store._in_clause(["1; DROP TABLE loan"])

    assert frag == "(%s)"
    assert params == ["1; DROP TABLE loan"]


# ================================================================ 业务库读取


def test_load_all_loans_without_where_clause():
    conn = FakeConn().queue_result([(1, 9, 5, 100.0, 80.0, 4.2, None, "2024-01-01")], LOAN_COLS)

    rows = store.load_loans(conn)

    sql, params = conn.executed[0]
    assert "WHERE" not in sql
    assert params == []
    assert rows[0]["loan_id"] == 1 and rows[0]["balance"] == 80.0


def test_load_specific_loans_filters_by_in_clause():
    conn = FakeConn().queue_result([], LOAN_COLS)

    store.load_loans(conn, [11, 22])

    sql, params = conn.executed[0]
    assert "WHERE loan_id IN (%s,%s)" in sql
    assert params == [11, 22]


def test_empty_id_list_short_circuits_without_querying():
    """CDC 增量拿到空变更集时，绝不能退化成全表扫描。"""
    conn = FakeConn()

    assert store.load_loans(conn, []) == []
    assert store.load_collaterals(conn, []) == {}
    assert store.load_customers(conn, []) == {}
    assert store.loans_by_collateral(conn, []) == []
    assert store.loans_by_customer(conn, []) == []
    assert conn.executed == []


def test_collaterals_indexed_by_primary_key():
    conn = FakeConn().queue_result(
        [(501, "广州市天河区", 23.1, 113.3, 100.0, 10, 1e7, 30.0, 25.0, 0, 0.0)],
        [
            "collateral_id",
            "property_addr",
            "lat",
            "lng",
            "area",
            "age",
            "true_market_price",
            "poi_density",
            "commute_min",
            "is_high_risk_zone",
            "spatial_feat_missing_pct",
        ],
    )

    got = store.load_collaterals(conn)

    assert set(got) == {501}
    assert got[501]["property_addr"] == "广州市天河区"


def test_collateral_missing_pct_scale_normalized_to_percent():
    """SPF-AC04：collateral.spatial_feat_missing_pct 落库是 0–1 小数，入口统一成 0–100。

    `store.load_collaterals()` 是全项目读 collateral 的唯一入口，0–1 → ×100 的换算
    放在这里，保证 risk_engine 拿到的永远是百分数标度、与 config.LOW_CONF_MISSING_PCT
    （75.0）同标度。修复前 `0.30 >= 25.0` 恒 False，AC-04 低置信路径全库从未触发。
    """
    conn = FakeConn().queue_result(
        [(501, "广州市天河区", 23.1, 113.3, 100.0, 10, 1e7, 30.0, 25.0, 0, 0.30)],
        [
            "collateral_id",
            "property_addr",
            "lat",
            "lng",
            "area",
            "age",
            "true_market_price",
            "poi_density",
            "commute_min",
            "is_high_risk_zone",
            "spatial_feat_missing_pct",
        ],
    )

    got = store.load_collaterals(conn)

    assert got[501]["spatial_feat_missing_pct"] == 30.0  # 0.30 → 30.0（0–100）


def test_collateral_missing_pct_already_percent_not_double_scaled():
    """入口幂等：已是 0–100 标度的值（如空间表覆盖过）不得再 ×100。"""
    conn = FakeConn().queue_result(
        [(501, "addr", None, None, 100.0, 10, 1e7, None, None, 0, 80.0)],
        [
            "collateral_id",
            "property_addr",
            "lat",
            "lng",
            "area",
            "age",
            "true_market_price",
            "poi_density",
            "commute_min",
            "is_high_risk_zone",
            "spatial_feat_missing_pct",
        ],
    )

    got = store.load_collaterals(conn)

    assert got[501]["spatial_feat_missing_pct"] == 80.0


def test_loans_looked_up_by_collateral():
    """抵押物估值变了，挂在它上面的所有贷款 LTV 都要重算。"""
    conn = FakeConn().queue_result([(101,), (102,)])

    assert store.loans_by_collateral(conn, [501]) == [101, 102]
    assert "FROM loan WHERE collateral_id IN (%s)" in conn.executed[0][0]


# ================================================================ 空间特征加载与覆盖


def test_spatial_features_load_latest_build_date_only():
    conn = FakeConn()
    conn.queue_result([(501, 30.0, 25.0, "z1", 5.0)])
    conn.queue_result([("z1", 1), ("z2", 0)])

    spatial, zone_risk = store.load_spatial(conn)

    assert "MAX(build_date)" in conn.executed[0][0]
    assert spatial[501]["zone_id"] == "z1"
    assert zone_risk == {"z1": 1, "z2": 0}


def test_null_missing_pct_treated_as_fully_missing():
    """missing_pct 为 NULL 说明这条空间特征没算出来，按 100% 缺失处理最保守。"""
    conn = FakeConn()
    conn.queue_result([(501, None, None, None, None)])
    conn.queue_result([])

    spatial, _ = store.load_spatial(conn)

    assert spatial[501]["spatial_feat_missing_pct"] == 100.0
    assert spatial[501]["poi_density"] is None


def test_missing_spatial_tables_degrade_to_empty_maps():
    """S3 没跑过时空间表不存在，风险引擎应继续用 collateral 占位字段而不是崩掉。"""
    assert store.load_spatial(RaisingConn()) == ({}, {})


def test_zone_hit_overrides_missing_pct_and_high_risk_flag(collateral):
    """zone_id 非空说明落在有样本的价格区块内，空间归属才可信。"""
    cols = {501: dict(collateral)}
    spatial = {
        501: {
            "poi_density": 88.0,
            "commute_min": 12.0,
            "zone_id": "z9",
            "spatial_feat_missing_pct": 40.0,
        }
    }

    store.apply_spatial(cols, spatial, {"z9": 1})

    assert cols[501]["poi_density"] == 88.0
    assert cols[501]["spatial_feat_missing_pct"] == 40.0
    assert cols[501]["is_high_risk_zone"] == 1


def test_zone_miss_still_overrides_missing_pct_but_not_high_risk_flag(collateral):
    """zone 未命中时：缺失率**照常覆盖**（SPF-AC04 修复），高危标记保持占位。

    修复前缺失率也受 zone 守卫限制——而 collateral 的 zone_id 生产上全空，导致
    真实空间缺失率（0–100）永远进不了引擎、AC-04 低置信永不触发（死代码）。
    修复后缺失率是空间特征自身的质量度量，与抵押物落不落网格无关：该落在网格里
    却没落上，恰恰说明空间信息不足、缺失率应当如实上报。
    高危标记仍受 zone 守卫：zone 为空（如种子随机坐标落在聚集区外）时维持占位值，
    否则整批贷款会被打成高危区。
    """
    cols = {501: dict(collateral)}
    spatial = {
        501: {
            "poi_density": 88.0,
            "commute_min": 12.0,
            "zone_id": None,
            "spatial_feat_missing_pct": 100.0,
        }
    }

    store.apply_spatial(cols, spatial, {})

    assert cols[501]["poi_density"] == 88.0  # 连续量仍然覆盖
    assert cols[501]["spatial_feat_missing_pct"] == 100.0  # 缺失率照常覆盖
    assert cols[501]["is_high_risk_zone"] == 0  # 高危标记保持占位


def test_unknown_zone_defaults_to_not_high_risk():
    cols = {501: {"is_high_risk_zone": 1, "spatial_feat_missing_pct": 0.0}}
    spatial = {
        501: {
            "poi_density": None,
            "commute_min": None,
            "zone_id": "z_unknown",
            "spatial_feat_missing_pct": 3.0,
        }
    }

    store.apply_spatial(cols, spatial, {})

    assert cols[501]["is_high_risk_zone"] == 0


def test_spatial_features_for_unknown_collateral_are_skipped(collateral):
    cols = {501: dict(collateral)}
    spatial = {
        999: {
            "poi_density": 1.0,
            "commute_min": 1.0,
            "zone_id": "z1",
            "spatial_feat_missing_pct": 1.0,
        }
    }

    store.apply_spatial(cols, spatial, {"z1": 1})  # 不应抛 KeyError

    assert cols[501]["poi_density"] == 30.0


# ================================================================ 批量打宽


def test_compute_rows_emits_one_row_per_loan_and_loss_for_orphans(loan, collateral):
    orphan = dict(loan, loan_id=1002, collateral_id=777)

    rows = store.compute_rows([loan, orphan], {501: collateral}, {}, {})

    assert len(rows) == 2
    assert rows[0]["risk_class"] == "正常"
    assert rows[1]["risk_class"] == "损失"  # 找不到抵押物


def test_compute_rows_applies_spatial_features_first(loan, collateral):
    """spatial 参数给了就必须先覆盖再算 LTV，否则用的是过期占位特征。"""
    spatial = (
        {
            501: {
                "poi_density": 1.0,
                "commute_min": 1.0,
                "zone_id": "z1",
                "spatial_feat_missing_pct": 90.0,
            }
        },
        {"z1": 1},
    )

    rows = store.compute_rows([loan], {501: collateral}, {}, {}, spatial=spatial)

    assert rows[0]["low_confidence"] is True  # 90% 缺失 → 低置信
    assert rows[0]["is_high_risk_zone"] == 1


# ================================================================ DWS 落库


def test_dws_tuple_normalizes_bools_and_missing_model_version():
    r = {
        "loan_id": 1,
        "customer_id": 2,
        "collateral_id": 3,
        "balance": 10.0,
        "interest_rate": 4.2,
        "market_valuation": 20.0,
        "ltv": 0.5,
        "risk_class": "正常",
        "low_confidence": False,
        "is_high_risk_zone": 0,
        "alert": True,
        "alert_level": "warn",
        "valuation_deviation_pct": None,
        "abnormal_valuation": None,
        "model_version": None,
    }

    t = store._dws_tuple(r)

    assert t[8] == 0  # low_confidence -> TINYINT
    assert t[10] == 1  # alert -> TINYINT
    assert t[11] == "warn"  # alert_level 原样透传
    assert t[13] == 0  # abnormal_valuation None -> 0
    assert t[14] == "unknown"  # 血缘不能落 NULL，缺失即 unknown


def test_dws_upsert_is_idempotent_per_loan_id():
    conn = FakeConn()
    rows = [dict(_dws_row(), loan_id=1), dict(_dws_row(), loan_id=2)]

    assert store.upsert_dws(conn, rows) == 2

    sql, sent = conn.executed_many[0]
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert len(sent) == 2
    assert conn.commits == 1


def test_empty_batch_writes_nothing():
    conn = FakeConn()

    assert store.upsert_dws(conn, []) == 0
    assert store.replace_alerts(conn, [], "2026-08-05") == 0
    assert store.delete_loans(conn, [], "2026-08-05") == 0
    assert conn.executed == [] and conn.executed_many == []


# ================================================================ 预警重写


def _dws_row(**kw):
    base = {
        "loan_id": 1,
        "customer_id": 2,
        "collateral_id": 3,
        "balance": 100.0,
        "interest_rate": 4.2,
        "market_valuation": 200.0,
        "ltv": 0.5,
        "risk_class": "正常",
        "low_confidence": False,
        "is_high_risk_zone": 0,
        "alert": False,
        "alert_level": None,
        "valuation_deviation_pct": None,
        "abnormal_valuation": False,
        "model_version": "s2-r4",
    }
    base.update(kw)
    return base


def test_alerts_rewrite_is_scoped_to_batch_and_date():
    """增量消费一笔时全表 DELETE 会误伤当日其它贷款的预警。"""
    conn = FakeConn()
    rows = [_dws_row(loan_id=1, alert=True), _dws_row(loan_id=2, alert=False)]

    assert store.replace_alerts(conn, rows, "2026-08-05") == 1

    sql, params = conn.find_sql("DELETE FROM ads_ltv_alerts")
    assert "alert_date=%s" in sql and "loan_id IN (%s,%s)" in sql
    assert params == ["2026-08-05", 1, 2]


def test_only_alerting_rows_reach_ltv_alert_table():
    conn = FakeConn()
    rows = [
        _dws_row(loan_id=1, alert=True, ltv=0.9, alert_level="strong"),
        _dws_row(loan_id=2, alert=False),
    ]

    store.replace_alerts(conn, rows, "2026-08-05")

    _, sent = conn.find_many("INSERT INTO ads_ltv_alerts")
    assert len(sent) == 1
    assert sent[0][0] == 1  # loan_id
    assert sent[0][8] == "strong"  # alert_level 随预警落库
    assert sent[0][-1] == "2026-08-05"  # alert_date 用批次日期


def test_missing_lineage_raises_traceability_alert():
    """R-UBQ-01：model_version 为 unknown → 估值结论不可溯源。"""
    conn = FakeConn()

    store.replace_alerts(conn, [_dws_row(model_version="unknown")], "2026-08-05")

    _, sent = conn.find_many("INSERT INTO ads_risk_valuation_alerts")
    assert [s[0] for s in sent] == ["R-UBQ-01"]


def test_abnormal_valuation_raises_manual_review_alert():
    conn = FakeConn()
    rows = [_dws_row(abnormal_valuation=True, valuation_deviation_pct=0.42)]

    store.replace_alerts(conn, rows, "2026-08-05")

    _, sent = conn.find_many("INSERT INTO ads_risk_valuation_alerts")
    assert sent[0][0] == "R-UNW-03"
    assert "42.00%" in sent[0][5]


def test_one_loan_can_raise_both_alert_codes():
    """两段独立累计——一笔贷款既不可溯源又偏差超标时要出两条，不能互相覆盖。"""
    conn = FakeConn()
    rows = [_dws_row(model_version="unknown", abnormal_valuation=True, valuation_deviation_pct=0.5)]

    store.replace_alerts(conn, rows, "2026-08-05")

    _, sent = conn.find_many("INSERT INTO ads_risk_valuation_alerts")
    assert sorted(s[0] for s in sent) == ["R-UBQ-01", "R-UNW-03"]


def test_review_alerts_cleaned_in_same_transaction_as_ltv_alerts():
    conn = FakeConn()

    store.replace_alerts(conn, [_dws_row()], "2026-08-05")

    assert conn.find_sql("DELETE FROM ads_risk_valuation_alerts", "alert_date=%s") is not None
    assert conn.commits == 1  # 两类告警共享一个事务，最后统一提交


def test_no_insert_issued_when_there_is_nothing_to_alert():
    conn = FakeConn()

    store.replace_alerts(conn, [_dws_row()], "2026-08-05")

    assert conn.find_many("INSERT INTO ads_risk_valuation_alerts") is None
    assert conn.find_many("INSERT INTO ads_ltv_alerts") is None


# ================================================================ 贷款删除


def test_deleting_loan_clears_detail_and_both_alert_tables():
    """只清一张表会留下「贷款已删但告警仍在」的幽灵敞口。"""
    conn = FakeConn()

    assert store.delete_loans(conn, [1, 2], "2026-08-05") == 2

    tables = [sql for sql, _ in conn.executed if sql.startswith("DELETE")]
    assert any("dws_risk_class" in s for s in tables)
    assert any("ads_ltv_alerts" in s for s in tables)
    assert any("ads_risk_valuation_alerts" in s for s in tables)


def test_deleting_dws_detail_is_not_date_filtered():
    """DWS 是当前快照（无日期分区），按日期删会留下陈旧行。"""
    conn = FakeConn()

    store.delete_loans(conn, [1], "2026-08-05")

    sql, params = conn.find_sql("DELETE FROM dws_risk_class")
    assert "alert_date" not in sql
    assert params == [1]


# ================================================================ 汇总表刷新


def test_summary_recomputed_from_full_dws_not_batch():
    """增量改一笔也要重刷：占比分母是全量余额，只更新本批会让占比失真。"""
    conn = FakeConn().queue_result([("正常", 3, 300.0), ("可疑", 1, 100.0)])

    out = store.refresh_ads_risk_class(conn, "2026-08-05")

    assert out["total_balance"] == 400.0
    assert out["by_class"]["正常"]["balance_pct"] == 0.75
    assert out["by_class"]["可疑"]["balance_pct"] == 0.25
    assert out["by_class"]["损失"]["count"] == 0  # 缺级补零


def test_summary_refresh_deletes_then_inserts_all_five_classes():
    conn = FakeConn().queue_result([("正常", 1, 100.0)])

    store.refresh_ads_risk_class(conn, "2026-08-05")

    sql, params = conn.find_sql("DELETE FROM ads_risk_class")
    assert params == ("2026-08-05",)
    _, sent = conn.find_many("INSERT INTO ads_risk_class")
    assert [s[1] for s in sent] == config.CLASS_ORDER


def test_summary_avoids_division_when_total_is_zero():
    conn = FakeConn().queue_result([("正常", 2, 0.0)])

    out = store.refresh_ads_risk_class(conn, "2026-08-05")

    assert out["total_balance"] == 0.0
    assert all(v["balance_pct"] == 0.0 for v in out["by_class"].values())


def test_summary_includes_out_of_vocabulary_classes():
    """⚠️ 与 risk_engine.build_aggregate 口径不一致：

    汇总表刷新（SQL 聚合）会把 dws_risk_class 里非五级的 risk_class 一并计入并写表，
    而 build_aggregate（内存聚合，risk_report.json 用）会静默丢弃它。
    同一批数据经两条路径会得出不同的总额与占比。这里钉住汇总表侧的行为。
    """
    conn = FakeConn().queue_result([("正常", 1, 100.0), ("疑似", 1, 900.0)])

    out = store.refresh_ads_risk_class(conn, "2026-08-05")

    assert "疑似" in out["by_class"]
    assert out["total_balance"] == 1000.0
    assert out["by_class"]["正常"]["balance_pct"] == 0.1


@pytest.mark.parametrize("fn", ["upsert_dws", "replace_alerts"])
def test_write_functions_commit_once(fn):
    conn = FakeConn()

    getattr(store, fn)(conn, [_dws_row()], "2026-08-05") if fn == "replace_alerts" else getattr(
        store, fn
    )(conn, [_dws_row()])

    assert conn.commits == 1
