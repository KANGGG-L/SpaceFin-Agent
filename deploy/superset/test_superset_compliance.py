#!/usr/bin/env python
"""Superset 投产加固合规测试（真机，skipif 同 I-04 模式）。

覆盖范围：
  1. Doris 脱敏视图：v_ltv_alerts_masked 的 customer_id 已按 data_classification.mask_value
     同口径脱敏（c****{后4位}）；5 个合规视图均存在。
  2. 审计钩子：以 admin 触发一次 /sqllab/ 请求后，spacefin.ads_export_audit 出现
     action='superset_query' 的审计记录（与驱动舱共用同一张审计表）。

运行：pytest deploy/superset/test_superset_compliance.py -v
前置：Doris(9030) / Superset(8088) / MySQL(3306) 在线；否则相应用例自动 skip。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pymysql
import pytest

# ---- 连接参数（与单一真源对齐）----
DORIS = {
    "host": os.getenv("DORIS_HOST", "127.0.0.1"),
    "port": int(os.getenv("DORIS_QUERY_PORT", "9030")),
    "user": "root",
    "password": os.getenv("DORIS_PASSWORD", ""),
}
SUPERSET_URL = os.getenv("SUPERSET_URL", "http://127.0.0.1:8088").rstrip("/")
ADMIN = os.getenv("SUPERSET_ADMIN", "admin")
PASSWORD = os.getenv("SUPERSET_PASSWORD", "superset_dev_only")
MYSQL = {
    "host": os.getenv("AUDIT_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("AUDIT_MYSQL_PORT", "3306")),
    "user": "root",
    "password": os.getenv("MYSQL_ROOT_PASSWORD", "spacefin_dev_only"),
    "database": "spacefin",
}

VIEWS = [
    ("ads", "v_risk_class"),
    ("ads", "v_avm_precision_trend"),
    ("ads", "v_city_avg_price"),
    ("ads", "v_compliance_audit"),
    ("ods", "v_ltv_alerts_masked"),
]


def _doris_ok() -> bool:
    try:
        c = pymysql.connect(**DORIS, charset="utf8mb4", connect_timeout=5)
        c.close()
        return True
    except Exception:
        return False


def _mysql_ok() -> bool:
    try:
        c = pymysql.connect(**MYSQL, charset="utf8mb4", connect_timeout=5)
        c.close()
        return True
    except Exception:
        return False


def _superset_login():
    body = json.dumps(
        {"username": ADMIN, "password": PASSWORD, "provider": "db", "refresh": True}
    ).encode()
    req = urllib.request.Request(
        f"{SUPERSET_URL}/api/v1/security/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())["access_token"]


def _superset_ok() -> bool:
    try:
        _superset_login()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _doris_ok(), reason="Doris 不可达，跳过脱敏视图断言")
def test_pii_views_exist():
    c = pymysql.connect(**DORIS, charset="utf8mb4")
    try:
        cur = c.cursor()
        for schema, view in VIEWS:
            cur.execute(f"SHOW TABLES FROM {schema} LIKE '{view}'")
            assert cur.fetchone() is not None, f"脱敏视图 {schema}.{view} 不存在"
    finally:
        c.close()


@pytest.mark.skipif(not _doris_ok(), reason="Doris 不可达，跳过脱敏形态断言")
def test_customer_id_masked():
    """v_ltv_alerts_masked.customer_id 必须为 c****{后4位} 形态（与 mask_value 同口径）。"""
    c = pymysql.connect(**DORIS, charset="utf8mb4")
    try:
        cur = c.cursor()
        cur.execute("SELECT customer_id FROM ods.v_ltv_alerts_masked LIMIT 10")
        rows = cur.fetchall()
        assert rows, "v_ltv_alerts_masked 无数据，无法验证脱敏形态"
        for (val,) in rows:
            s = str(val)
            assert s.startswith("c****"), f"customer_id 未脱敏: {s!r}"
            # 口径：c****(5 字符) + 后 4 位 = 9 字符；原值 ≤4 位时退化为 c****(4 字符)。
            assert len(s) in (4, 9), f"脱敏形态异常(应 c**** 或 c****+4位): {s!r}"
            if len(s) == 9:
                assert s[5:].isdigit(), f"脱敏后缀非数字: {s!r}"
    finally:
        c.close()


@pytest.mark.skipif(
    not (_superset_ok() and _mysql_ok()),
    reason="Superset 或 MySQL 不可达，跳过审计钩子断言",
)
def test_audit_hook_writes():
    """以 admin 触发 /sqllab/ 后，ads_export_audit 出现 superset_query 审计记录。"""
    token = _superset_login()
    # 触发一次合规相关请求（/sqllab/ 在审计前缀内，且为正常成功请求，必走 after_request）。
    req = urllib.request.Request(
        f"{SUPERSET_URL}/sqllab/",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except urllib.error.HTTPError:
        pass  # 即使页面异常，after_request 钩子仍应写入审计

    c = pymysql.connect(**MYSQL, charset="utf8mb4")
    try:
        cur = c.cursor()
        cur.execute(
            "SELECT action, username, role FROM ads_export_audit "
            "WHERE username=%s AND action='superset_query' "
            "AND created_at >= NOW() - INTERVAL 1 MINUTE "
            "ORDER BY id DESC LIMIT 1",
            (ADMIN,),
        )
        row = cur.fetchone()
        assert row is not None, "触发 /sqllab/ 后 ads_export_audit 未出现 superset_query 记录"
        assert row[1] == ADMIN
    finally:
        c.close()
