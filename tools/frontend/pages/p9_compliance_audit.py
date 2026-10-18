#!/usr/bin/env python
"""P9 · 合规审计（面向合规官）。

这一页回答两个问题：**谁在什么时候干了什么**、**报送被阻断过几次**。

- 导出审计：直读 `ads_export_audit`（TC-06：who/role/when/what/result/ip）。
  这张表由前端启动时幂等创建、导出/确认/配置变更写审计，天然就是「PII 导出脱敏记录」
  的落点——detail 里的 rows 字段即导出行数，脱敏规则见 LTV 预警导出（客户号只留后 4 位）。
  支持按日期范围、操作类型、用户名筛选。
- 报送告警：展示 `ads_report_alert` 中所有级别的告警（block/warn/info），
  对应设计评审 §2.2 报送状态机里的「已阻断」态（AC-05 / AC-08 的界面证据）。
  支持按级别、日期范围筛选。

数据全是真实运行态，无任何 mock：审计表有数据是导出/确认动作的留痕，报送告警有数据是
口径校验被触发过；两者为空则如实显示空态。

RBAC：admin / risk 可见（合规审计属风控与管理员职责，贷后与 DA 不接触审计明细）。

> 特征归因（AVM 为何这么估）已移至 P7 AVM 估值管理页，本页不再包含。
"""

import json
import os
import sys

_FRONTEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _FRONTEND_DIR not in sys.path:
    sys.path.insert(0, _FRONTEND_DIR)

import db  # noqa: E402

# 展示上限
PAGE_LIMIT = 200

# 动作码 → 中文说明
ACTION_LABELS = {
    "export": "导出预警 CSV",
    "confirm": "预警确认",
    "dispose": "预警处置",
    "datasource_toggle": "数据源开关变更",
    "policy_save": "策略保存",
    "policy_delete": "策略删除",
    "policy_preview": "策略预览",
    "persona_refresh": "校准重算",
    "avm_attr_refresh": "归因重建",
    "report_submit": "1104 报送",
    "report_rebuild": "重建报送快照",
}


def _detail_rows(detail_text):
    """从审计 detail（JSON 文本）里抽出 rows 字段（导出/试算行数），取不到返回 None。"""
    if not detail_text:
        return None
    try:
        parsed = json.loads(detail_text)
    except ValueError:
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("rows"), (int, float)):
        return int(parsed["rows"])
    return None


def _build_where(conditions, values):
    """拼装 WHERE 子句，返回 (WHERE 片段, 参数列表)。

    conditions 为空时返回 ('', [])，即无 WHERE 条件。
    """
    if conditions:
        return "WHERE " + " AND ".join(conditions), values
    return "", []


def get_compliance_audit(ctx):
    """两块数据：导出审计 + 报送告警。支持筛选。"""
    conn = db.crawl_conn()
    try:
        cur = conn.cursor()

        # ---- 1. 导出审计（支持筛选） ----
        export_conditions = []
        export_params = []

        date_from = ctx.query.get("date_from", [None])[0]
        date_to = ctx.query.get("date_to", [None])[0]
        action_filter = ctx.query.get("action", [None])[0]
        user_filter = ctx.query.get("user", [None])[0]

        if date_from:
            export_conditions.append("created_at >= %s")
            export_params.append(date_from)
        if date_to:
            export_conditions.append("created_at <= %s")
            export_params.append(date_to + " 23:59:59")
        if action_filter:
            export_conditions.append("action = %s")
            export_params.append(action_filter)
        if user_filter:
            export_conditions.append("username LIKE %s")
            export_params.append(f"%{user_filter}%")

        export_where, export_params = _build_where(export_conditions, export_params)

        cur.execute(f"SELECT COUNT(*) FROM ads_export_audit {export_where}", export_params)
        export_total = int(cur.fetchone()[0])

        cur.execute(
            f"SELECT id, action, username, role, detail, result, ip, created_at "
            f"FROM ads_export_audit {export_where} ORDER BY id DESC LIMIT %s",
            export_params + [PAGE_LIMIT],
        )
        cols = [d[0] for d in cur.description]
        export_rows = []
        for r in cur.fetchall():
            row = dict(zip(cols, r, strict=True))
            row["action_label"] = ACTION_LABELS.get(row["action"], row["action"])
            row["rows"] = _detail_rows(row["detail"])
            row["created_at"] = str(row["created_at"])
            export_rows.append(row)

        # ---- 2. 报送告警（所有级别，支持筛选） ----
        alert_conditions = []
        alert_params = []

        alert_level = ctx.query.get("alert_level", [None])[0]
        alert_date_from = ctx.query.get("alert_date_from", [None])[0]
        alert_date_to = ctx.query.get("alert_date_to", [None])[0]

        if alert_level:
            alert_conditions.append("alert_level = %s")
            alert_params.append(alert_level)
        if alert_date_from:
            alert_conditions.append("report_date >= %s")
            alert_params.append(alert_date_from)
        if alert_date_to:
            alert_conditions.append("report_date <= %s")
            alert_params.append(alert_date_to)

        alert_where, alert_params = _build_where(alert_conditions, alert_params)

        cur.execute(f"SELECT COUNT(*) FROM ads_report_alert {alert_where}", alert_params)
        report_alert_total = int(cur.fetchone()[0])

        cur.execute(
            f"SELECT id, report_date, report_type, alert_level, check_name, detail, etl_ts "
            f"FROM ads_report_alert {alert_where} ORDER BY id DESC LIMIT %s",
            alert_params + [PAGE_LIMIT],
        )
        cols = [d[0] for d in cur.description]
        report_alerts = []
        for r in cur.fetchall():
            row = dict(zip(cols, r, strict=True))
            row["report_date"] = str(row["report_date"])
            row["etl_ts"] = str(row["etl_ts"])
            report_alerts.append(row)

        cur.close()
    finally:
        conn.close()

    return {
        "export_rows": export_rows,
        "export_total": export_total,
        "report_alerts": report_alerts,
        "report_alert_total": report_alert_total,
    }


PAGE = {
    "id": "compliance_audit",
    "label": "合规审计",
    "roles": {"admin"},
    "order": 90,
    "js": "p9_compliance_audit.js",
    "routes": {
        ("GET", "/api/compliance_audit"): get_compliance_audit,
    },
}
