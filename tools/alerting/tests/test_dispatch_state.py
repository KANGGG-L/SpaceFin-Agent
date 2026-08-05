"""LTV 预警推送状态机（I-05 / TC-03）：T+1 去重、失败重试、终态失败。

状态机语义（tools/alerting/main.py 顶部约定）：
    pending ──▶ success（记 dispatch_ts）
        └失败──▶ failed, attempt_count+1
                 ├─ attempt_count < max_retries ──▶ 下一轮自动重试
                 └─ attempt_count >= max_retries ──▶ 留 failed（终态，待人工）

这一层的核心承诺是「任何一天重复执行都不会把同一条预警推两遍」，
所以去重与重试边界必须逐个钉死。
"""

import alertmods
import pytest
from alertmods import alert_row, alerting, dispatch_row


def run(conn, drivers, date="2026-08-05", max_retries=3, force_fail=False):
    return alerting.run_dispatch(conn, drivers, date, max_retries, force_fail)


# ================================================================ 首轮推送


def test_first_round_pushes_all_candidates(make_conn, driver):
    conn = make_conn([alert_row(1), alert_row(2)])

    s = run(conn, [driver])

    assert s["candidates"] == 2
    assert s["pushed"] == 2
    assert s["attempted"] == 2
    assert [a["loan_id"] for a in driver.sent] == [1, 2]


def test_first_success_recorded_with_attempt_one(make_conn, driver):
    conn = make_conn([alert_row(1)])

    run(conn, [driver])

    _, params = conn.find_sql("INSERT INTO ads_alert_dispatch")
    assert params[0] == 1  # loan_id
    assert params[3] == alerting.STATE_SUCCESS
    assert params[4] == 1  # attempt_count
    assert params[6] == ""  # last_error 为空


def test_dispatch_ledger_upserts_on_loan_and_alert_date(make_conn, driver):
    """UNIQUE(loan_id, alert_date) + UPSERT：同一预警重推只刷新一行。"""
    conn = make_conn([alert_row(1)])

    run(conn, [driver])

    sql, _ = conn.find_sql("INSERT INTO ads_alert_dispatch")
    assert "ON DUPLICATE KEY UPDATE" in sql


def test_every_driver_receives_every_alert(make_conn):
    a, b = alertmods.RecordingDriver(), alertmods.RecordingDriver()
    conn = make_conn([alert_row(1)])

    run(conn, [a, b])

    assert len(a.sent) == 1 and len(b.sent) == 1


def test_empty_candidate_list_pushes_nothing(make_conn, driver):
    conn = make_conn([])

    s = run(conn, [driver])

    assert s["candidates"] == 0
    assert driver.sent == []
    assert conn.find_sql("INSERT INTO ads_alert_dispatch") is None


# ================================================================ T+1 去重


def test_already_delivered_alert_is_deduplicated(make_conn, driver):
    """TC-03 的核心承诺：T 日已送达，T+1 再跑不能推第二遍。"""
    conn = make_conn([alert_row(1)], [dispatch_row(1, "success", 1)])

    s = run(conn, [driver])

    assert s["dedup"] == 1
    assert s.get("pushed", 0) == 0
    assert driver.sent == []


def test_dedup_is_scoped_per_alert_date(make_conn, driver):
    """同一 loan_id 在不同 alert_date 是两条独立预警，昨天送过不能屏蔽今天的。"""
    conn = make_conn(
        [alert_row(1, date="2026-08-05")],
        [dispatch_row(1, "success", 1, date="2026-08-04")],
    )

    s = run(conn, [driver], date="2026-08-05")

    assert s.get("dedup", 0) == 0
    assert s["pushed"] == 1


def test_only_undelivered_alerts_are_pushed(make_conn, driver):
    conn = make_conn(
        [alert_row(1), alert_row(2), alert_row(3)],
        [dispatch_row(1, "success", 1)],
    )

    s = run(conn, [driver])

    assert s["dedup"] == 1
    assert s["pushed"] == 2
    assert [a["loan_id"] for a in driver.sent] == [2, 3]


# ================================================================ 失败与重试


def test_failure_records_failed_status_and_error(make_conn):
    conn = make_conn([alert_row(1)])
    driver = alertmods.RecordingDriver(fail_on=[1])

    s = run(conn, [driver])

    assert s["failed"] == 1
    assert s.get("pushed", 0) == 0
    _, params = conn.find_sql("INSERT INTO ads_alert_dispatch")
    assert params[3] == alerting.STATE_FAILED
    assert params[4] == 1
    assert "下游拒收" in params[6]  # last_error 留痕


def test_failed_alert_is_retried_with_incremented_attempt(make_conn, driver):
    conn = make_conn([alert_row(1)], [dispatch_row(1, "failed", 1)])

    s = run(conn, [driver])

    assert s["retried"] == 1
    assert s["pushed"] == 1
    _, params = conn.find_sql("INSERT INTO ads_alert_dispatch")
    assert params[4] == 2  # attempt_count 从 1 递增到 2


@pytest.mark.parametrize("attempts", [1, 2])
def test_alert_below_retry_limit_is_retried(make_conn, driver, attempts):
    conn = make_conn([alert_row(1)], [dispatch_row(1, "failed", attempts, max_retries=3)])

    s = run(conn, [driver])

    assert s["retried"] == 1
    assert s.get("final_failed", 0) == 0


def test_attempts_at_limit_become_terminal_failure(make_conn, driver):
    """边界：attempt_count >= max_retries 就停手，留给人工，不无限重试。"""
    conn = make_conn([alert_row(1)], [dispatch_row(1, "failed", 3, max_retries=3)])

    s = run(conn, [driver])

    assert s["final_failed"] == 1
    assert s.get("attempted", 0) == 0
    assert driver.sent == []


def test_attempts_over_limit_stay_terminal(make_conn, driver):
    conn = make_conn([alert_row(1)], [dispatch_row(1, "failed", 9, max_retries=3)])

    assert run(conn, [driver])["final_failed"] == 1


def test_total_attempts_equal_max_retries(make_conn):
    """连续三轮全失败后第四轮进终态：总共真正尝试 3 次 == max_retries。"""
    attempts_seen = []
    for prev_attempts in (None, 1, 2, 3):
        dispatch = [] if prev_attempts is None else [dispatch_row(1, "failed", prev_attempts)]
        conn = make_conn([alert_row(1)], dispatch)
        driver = alertmods.RecordingDriver(fail_on=[1])
        s = run(conn, [driver], max_retries=3)
        if s.get("attempted"):
            attempts_seen.append(conn.find_sql("INSERT INTO ads_alert_dispatch")[1][4])

    assert attempts_seen == [1, 2, 3]  # 第四轮没有产生新的尝试


def test_terminal_check_uses_stored_max_retries_not_cli_arg(make_conn, driver):
    """⚠️ 语义注意：判定读的是 prev['max_retries']（历史落库值），

    不是本轮 --max-retries。临时调高 CLI 参数并不能让已终态的记录复活，
    需要改台账。这是刻意行为还是遗漏值得确认，先钉住现状。
    """
    conn = make_conn([alert_row(1)], [dispatch_row(1, "failed", 2, max_retries=2)])

    s = run(conn, [driver], max_retries=99)

    assert s["final_failed"] == 1


def test_one_failure_does_not_block_the_rest_of_batch(make_conn):
    """一条推不动不能拖垮整批——贷后预警是逐条送达语义。"""
    conn = make_conn([alert_row(1), alert_row(2), alert_row(3)])
    driver = alertmods.RecordingDriver(fail_on=[2])

    s = run(conn, [driver])

    assert s["pushed"] == 2
    assert s["failed"] == 1
    assert [a["loan_id"] for a in driver.sent] == [1, 3]


# ================================================================ 多驱动下的部分投递


def test_any_driver_failure_fails_whole_alert(make_conn):
    conn = make_conn([alert_row(1)])
    ok, bad = alertmods.RecordingDriver(), alertmods.RecordingDriver(fail_on=[1])

    s = run(conn, [ok, bad])

    assert s["failed"] == 1


def test_partial_delivery_causes_duplicate_push_on_retry(make_conn):
    """⚠️ 已知语义缺口：驱动按列表顺序串行，失败即 break，整条记 failed 重试。

    于是下一轮会把**所有**驱动重推一遍。site_inbox 靠 UNIQUE 键幂等没事，
    但 FileDriver 是纯 append，同一条预警会在 JSONL 里出现多行。
    这里钉住行为：第一个驱动确实收到了两次。
    """
    ok, bad = alertmods.RecordingDriver(), alertmods.RecordingDriver(fail_on=[1])

    conn = alertmods.FakeConn()
    conn.on("FROM ads_ltv_alerts", [alert_row(1)], alertmods.ALERT_COLS)
    conn.on("FROM ads_alert_dispatch", [])
    run(conn, [ok, bad])

    conn2 = alertmods.FakeConn()
    conn2.on("FROM ads_ltv_alerts", [alert_row(1)], alertmods.ALERT_COLS)
    conn2.on("FROM ads_alert_dispatch", [dispatch_row(1, "failed", 1)])
    run(conn2, [ok, bad])

    assert len(ok.sent) == 2  # 同一条预警被成功通道投递了两次


def test_drivers_after_a_failure_are_skipped_this_round(make_conn):
    """break 语义：排在失败驱动后面的通道本轮完全不会收到。"""
    conn = make_conn([alert_row(1)])
    bad, later = alertmods.RecordingDriver(fail_on=[1]), alertmods.RecordingDriver()

    run(conn, [bad, later])

    assert later.sent == []


# ================================================================ 演练开关


def test_force_fail_raises_before_touching_drivers(make_conn, driver):
    """--force-fail 用于验收重试状态机，必须在调用驱动之前就抛，避免污染下游。"""
    conn = make_conn([alert_row(1), alert_row(2)])

    s = run(conn, [driver], force_fail=True)

    assert s["failed"] == 2
    assert driver.sent == []
    _, params = conn.find_sql("INSERT INTO ads_alert_dispatch")
    assert "演练注入" in params[6]


def test_force_fail_still_feeds_the_retry_state_machine(make_conn, driver):
    conn = make_conn([alert_row(1)], [dispatch_row(1, "failed", 1)])

    s = run(conn, [driver], force_fail=True)

    assert s["retried"] == 1
    assert s["failed"] == 1


# ================================================================ 摘要口径


def test_summary_omits_counters_that_never_fired(make_conn, driver):
    """⚠️ Counter 转 dict：没发生的事件不会出现在摘要里（如全成功时无 'failed' 键）。

    下游读摘要 JSON 必须用 .get 而不是下标，否则全绿的那天反而 KeyError。
    """
    conn = make_conn([alert_row(1)])

    s = run(conn, [driver])

    assert "failed" not in s
    assert "dedup" not in s
    assert set(s) >= {"candidates", "pushed", "attempted", "retried"}


def test_candidate_count_includes_deduplicated_alerts(make_conn, driver):
    conn = make_conn([alert_row(1), alert_row(2)], [dispatch_row(1, "success", 1)])

    s = run(conn, [driver])

    assert s["candidates"] == 2
    assert s["dedup"] + s["pushed"] == 2


def test_ledger_lookup_is_scoped_to_batch_loans(make_conn, driver):
    conn = make_conn([alert_row(7), alert_row(8)])

    run(conn, [driver])

    sql, params = conn.find_sql("FROM ads_alert_dispatch")
    assert "loan_id IN (%s,%s)" in sql
    assert params == [7, 8]


def test_ledger_lookup_is_not_scoped_by_alert_date(make_conn, driver):
    """⚠️ 现状：只按 loan_id 过滤，会把该贷款历史所有日期的台账行都拉回来。

    结果字典以 (loan_id, alert_date) 为键，判定仍然正确，但长期运行的贷款
    会白拉很多行。属性能问题而非正确性问题，记录备查。
    """
    conn = make_conn([alert_row(1)])

    run(conn, [driver])

    sql, _ = conn.find_sql("FROM ads_alert_dispatch")
    assert "alert_date=" not in sql


def test_alert_list_filtered_by_date_and_ordered_by_id(make_conn, driver):
    conn = make_conn([alert_row(1)])

    run(conn, [driver], date="2026-08-05")

    sql, params = conn.find_sql("FROM ads_ltv_alerts")
    assert "WHERE alert_date=%s" in sql and "ORDER BY id" in sql
    assert params == ("2026-08-05",)
