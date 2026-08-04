"""风险结果读写层：全量重算与 CDC 增量重算共用同一套加载 / 落库语义。

拆出本模块的原因：CDC 消费链要按 loan_id 做**局部**重算，而 tools/risk/main.py 原先是
「DELETE 全表 + 全量 INSERT」。两套 SQL 各写一遍必然漂移（字段顺序、五级分类口径、
预警去重规则），一旦漂移，增量结果与全量结果就对不上，验收时无法互证。故统一收口于此：
main.py 与 tools/cdc/consumer.py 都只调用这里的函数。

关键语义约定：
- `upsert_dws` 用 ON DUPLICATE KEY UPDATE，按 loan_id 幂等覆盖，增量与全量结果一致。
- `replace_alerts` 只删「本批 loan_id + 当日」的预警再插入，不清全表——否则增量消费一次
  就会把当日其它贷款的预警抹掉。
- `refresh_ads_risk_class` 从 dws_risk_class 现状聚合，而不是从本批 rows 聚合：汇总表是
  全量口径，增量改一笔也必须让占比重新对齐全量分母。
"""

from __future__ import annotations

import config
import risk_engine

DWS_INSERT_SQL = (
    "INSERT INTO dws_risk_class "
    "(loan_id, customer_id, collateral_id, balance, interest_rate, market_valuation, "
    " ltv, risk_class, low_confidence, is_high_risk_zone, alert) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    " customer_id=VALUES(customer_id), collateral_id=VALUES(collateral_id), "
    " balance=VALUES(balance), interest_rate=VALUES(interest_rate), "
    " market_valuation=VALUES(market_valuation), ltv=VALUES(ltv), "
    " risk_class=VALUES(risk_class), low_confidence=VALUES(low_confidence), "
    " is_high_risk_zone=VALUES(is_high_risk_zone), alert=VALUES(alert), "
    " etl_ts=CURRENT_TIMESTAMP"
)

ALERT_INSERT_SQL = (
    "INSERT INTO ads_ltv_alerts "
    "(loan_id, customer_id, collateral_id, loan_balance, market_valuation, ltv, "
    " risk_class, is_high_risk_zone, alert_date) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)


# ---------------------------------------------------------------- 业务库读取


def _in_clause(ids: list) -> tuple[str, list]:
    """构造 IN (%s,%s,...) 片段；ids 为空时调用方应短路，不要走到这里。"""
    return "(" + ",".join(["%s"] * len(ids)) + ")", list(ids)


def load_loans(conn, loan_ids: list | None = None) -> list[dict]:
    """读贷款台账；loan_ids 为 None 取全量，否则只取指定几笔（CDC 增量路径）。"""
    sql = (
        "SELECT loan_id, customer_id, collateral_id, loan_amount, balance, "
        "interest_rate, risk_class, origination_date FROM loan"
    )
    params: list = []
    if loan_ids is not None:
        if not loan_ids:
            return []
        frag, params = _in_clause(loan_ids)
        sql += f" WHERE loan_id IN {frag}"
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    cur.close()
    return rows


def load_collaterals(conn, collateral_ids: list | None = None) -> dict:
    """读抵押物主档，返回 {collateral_id: row}。"""
    sql = (
        "SELECT collateral_id, property_addr, lat, lng, area, age, true_market_price, "
        "poi_density, commute_min, is_high_risk_zone, spatial_feat_missing_pct FROM collateral"
    )
    params: list = []
    if collateral_ids is not None:
        if not collateral_ids:
            return {}
        frag, params = _in_clause(collateral_ids)
        sql += f" WHERE collateral_id IN {frag}"
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    cur.close()
    return {r["collateral_id"]: r for r in rows}


def load_customers(conn, customer_ids: list | None = None) -> dict:
    """读客户主档，返回 {customer_id: row}。"""
    sql = "SELECT customer_id, credit_score, income_monthly, debt_ratio FROM customer"
    params: list = []
    if customer_ids is not None:
        if not customer_ids:
            return {}
        frag, params = _in_clause(customer_ids)
        sql += f" WHERE customer_id IN {frag}"
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    cur.close()
    return {r["customer_id"]: r for r in rows}


def loans_by_collateral(conn, collateral_ids: list) -> list[int]:
    """抵押物变更 → 受影响贷款：估值变了，挂在它上面的贷款 LTV 全部要重算。"""
    if not collateral_ids:
        return []
    frag, params = _in_clause(collateral_ids)
    cur = conn.cursor()
    cur.execute(f"SELECT loan_id FROM loan WHERE collateral_id IN {frag}", params)
    out = [r[0] for r in cur.fetchall()]
    cur.close()
    return out


def loans_by_customer(conn, customer_ids: list) -> list[int]:
    """客户变更 → 受影响贷款（当前风险口径未直接用客户特征，仍重算以保持视图新鲜）。"""
    if not customer_ids:
        return []
    frag, params = _in_clause(customer_ids)
    cur = conn.cursor()
    cur.execute(f"SELECT loan_id FROM loan WHERE customer_id IN {frag}", params)
    out = [r[0] for r in cur.fetchall()]
    cur.close()
    return out


# ---------------------------------------------------------------- 计算


def compute_rows(
    loans: list[dict], collaterals: dict, customers: dict, dwd_unit: dict, avm_model=None
) -> list:
    """对一批贷款做打宽（估值 → LTV → 五级 → 预警）。增量与全量走同一函数。

    avm_model 为 None 时跳过 AVM 估值（无模型环境与合成地址场景兼容）。
    """
    return [
        risk_engine.enrich_loan(
            ln,
            collaterals.get(ln["collateral_id"]),
            customers.get(ln["customer_id"]),
            dwd_unit,
            config.CITY_MAP,
            avm_model=avm_model,
        )
        for ln in loans
    ]


# ---------------------------------------------------------------- 落库


def ensure_ads_tables(conn) -> None:
    """幂等建 DWS/ADS 表（DDL 需 root，见 config.root_crawl_params）。"""
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dws_risk_class (
            loan_id INT PRIMARY KEY,
            customer_id INT, collateral_id INT,
            balance DECIMAL(14,2), interest_rate DECIMAL(5,2),
            market_valuation DECIMAL(14,2), ltv DECIMAL(8,4),
            risk_class VARCHAR(8), low_confidence TINYINT,
            is_high_risk_zone TINYINT, alert TINYINT,
            etl_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ads_ltv_alerts (
            id INT AUTO_INCREMENT PRIMARY KEY,
            loan_id INT, customer_id INT, collateral_id INT,
            loan_balance DECIMAL(14,2), market_valuation DECIMAL(14,2),
            ltv DECIMAL(8,4), risk_class VARCHAR(8),
            is_high_risk_zone TINYINT, alert_date DATE,
            etl_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            KEY idx_loan (loan_id), KEY idx_date (alert_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ads_risk_class (
            stat_date DATE, risk_class VARCHAR(8),
            loan_count INT, balance_total DECIMAL(16,2), balance_pct DECIMAL(8,4),
            etl_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (stat_date, risk_class)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    conn.commit()
    cur.close()


def _dws_tuple(r: dict) -> tuple:
    return (
        r["loan_id"],
        r["customer_id"],
        r["collateral_id"],
        r["balance"],
        r["interest_rate"],
        r["market_valuation"],
        r["ltv"],
        r["risk_class"],
        int(r["low_confidence"]),
        r["is_high_risk_zone"],
        int(r["alert"]),
    )


def upsert_dws(conn, rows: list[dict]) -> int:
    """按 loan_id UPSERT 打宽明细；返回处理行数。"""
    if not rows:
        return 0
    cur = conn.cursor()
    cur.executemany(DWS_INSERT_SQL, [_dws_tuple(r) for r in rows])
    conn.commit()
    cur.close()
    return len(rows)


def replace_alerts(conn, rows: list[dict], date: str) -> int:
    """只重写本批 loan_id 在 `date` 当日的预警：先删后插。

    不做全表 DELETE：增量消费一次只该影响这几笔，全表删会误伤当日其它预警。
    """
    if not rows:
        return 0
    loan_ids = [r["loan_id"] for r in rows]
    frag, params = _in_clause(loan_ids)
    cur = conn.cursor()
    cur.execute(
        f"DELETE FROM ads_ltv_alerts WHERE alert_date=%s AND loan_id IN {frag}",
        [date, *params],
    )
    alerts = [r for r in rows if r["alert"]]
    if alerts:
        cur.executemany(
            ALERT_INSERT_SQL,
            [
                (
                    r["loan_id"],
                    r["customer_id"],
                    r["collateral_id"],
                    r["balance"],
                    r["market_valuation"],
                    r["ltv"],
                    r["risk_class"],
                    r["is_high_risk_zone"],
                    date,
                )
                for r in alerts
            ],
        )
    conn.commit()
    cur.close()
    return len(alerts)


def delete_loans(conn, loan_ids: list, date: str) -> int:
    """贷款被删除（CDC DELETE）：清掉其 DWS 明细与当日预警，避免下游看到幽灵敞口。"""
    if not loan_ids:
        return 0
    frag, params = _in_clause(loan_ids)
    cur = conn.cursor()
    cur.execute(f"DELETE FROM dws_risk_class WHERE loan_id IN {frag}", params)
    cur.execute(
        f"DELETE FROM ads_ltv_alerts WHERE alert_date=%s AND loan_id IN {frag}",
        [date, *params],
    )
    conn.commit()
    cur.close()
    return len(loan_ids)


def refresh_ads_risk_class(conn, date: str) -> dict:
    """从 dws_risk_class 现状重算当日五级分类汇总（全量口径，SQL 聚合，非逐笔重算）。

    增量改一笔也要刷新它：占比的分母是全量余额，只更新本批会让占比失真。
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT risk_class, COUNT(*), COALESCE(SUM(balance),0) FROM dws_risk_class GROUP BY risk_class"
    )
    got = {r[0]: (int(r[1]), float(r[2])) for r in cur.fetchall()}
    by_class = {cls: {"count": 0, "balance": 0.0} for cls in config.CLASS_ORDER}
    for cls, (cnt, bal) in got.items():
        by_class.setdefault(cls, {"count": 0, "balance": 0.0})
        by_class[cls] = {"count": cnt, "balance": round(bal, 2)}
    total = sum(v["balance"] for v in by_class.values())
    for v in by_class.values():
        v["balance_pct"] = round(v["balance"] / total, 4) if total else 0.0

    cur.execute("DELETE FROM ads_risk_class WHERE stat_date=%s", (date,))
    cur.executemany(
        "INSERT INTO ads_risk_class (stat_date, risk_class, loan_count, balance_total, balance_pct) "
        "VALUES (%s,%s,%s,%s,%s)",
        [(date, cls, v["count"], v["balance"], v["balance_pct"]) for cls, v in by_class.items()],
    )
    conn.commit()
    cur.close()
    return {"by_class": by_class, "total_balance": round(total, 2)}
