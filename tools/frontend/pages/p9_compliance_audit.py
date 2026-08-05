#!/usr/bin/env python
"""P9 · 合规审计 / 特征归因（设计评审 P9 / R-UNW-02，面向合规官）。

这一页回答三个问题：**谁在什么时候干了什么**、**报送被阻断过几次**、**AVM 估值
为什么这么估**。

- 导出审计：直读 `ads_export_audit`（TC-06：who/role/when/what/result/ip）。
  这张表由前端启动时幂等创建、导出/确认/配置变更写审计，天然就是「PII 导出脱敏记录」
  的落点——detail 里的 rows 字段即导出行数，脱敏规则见 LTV 预警导出（客户号只留后 4 位）。
- 报送告警：只取 `ads_report_alert` 中 `alert_level='block'` 的阻断级记录，
  对应设计评审 §2.2 报送状态机里的「已阻断」态（AC-05 / AC-08 的界面证据）。
- 特征归因：读取 `output/avm/attribution_report.json`（dev-attrib 本轮产出的 permutation importance 归因）。
  归因产物在 output/ 下（.gitignore，有意为之），**文件不存在是正常路径**——页面降级
  显示「归因报告未生成，请先运行 tools/avm 训练」，绝不 500。

数据全是真实运行态，无任何 mock：审计表有数据是导出/确认动作的留痕，报送告警有数据是
口径校验被触发过；两者为空则如实显示空态。

RBAC：admin / risk 可见（合规审计属风控与管理员职责，贷后与 DA 不接触审计明细）。
"""

import json
import os
import sys

# pages 以包形式被导入时 frontend 根目录不一定在 sys.path 上（取决于启动方式），
# 这里补一次，保证 `import db` 在 app.py 直跑与 `python -c "import pages"` 两种场景都成立。
_FRONTEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _FRONTEND_DIR not in sys.path:
    sys.path.insert(0, _FRONTEND_DIR)

import db  # noqa: E402

# 仓库根：本文件在 <root>/tools/frontend/pages/ 下，回退四层。
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
# 特征归因产物：dev-attrib 本轮产出（permutation importance），与 avm_report.json 同目录。
_ATTRIBUTION_FILE = os.path.join(_REPO_ROOT, "output", "avm", "attribution_report.json")

# 展示上限：审计明细是留痕流水，只显示最近一批即可，完整追溯交给 DBA。
PAGE_LIMIT = 200

# 动作码 → 中文说明（前端同源维护中文展示，这里只做后端聚合不需要）。
ACTION_LABELS = {
    "export": "导出预警 CSV",
    "confirm": "预警确认",
    "datasource_toggle": "数据源开关变更",
    "policy_save": "策略保存",
    "policy_delete": "策略删除",
}


def _load_json(path):
    """读一个 JSON 产物；文件缺失/损坏返回 None（页面降级展示，不 500）。

    归因报告在 output/ 下（.gitignore），换台机器 clone 完就是没有的，
    因此「读不到」是正常路径而非异常路径，绝不能让它冒泡成 500。
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


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


def get_compliance_audit(ctx):
    """三块数据：导出审计 / 报送阻断告警 / 特征归因报告。"""
    conn = db.crawl_conn()
    try:
        cur = conn.cursor()

        # ---- 1. 导出审计（TC-06 / R-UNW-02：PII 导出脱敏留痕） ----
        cur.execute("SELECT COUNT(*) FROM ads_export_audit")
        export_total = int(cur.fetchone()[0])
        cur.execute(
            "SELECT id, action, username, role, detail, result, ip, created_at "
            "FROM ads_export_audit ORDER BY id DESC LIMIT %s",
            (PAGE_LIMIT,),
        )
        cols = [d[0] for d in cur.description]
        export_rows = []
        for r in cur.fetchall():
            row = dict(zip(cols, r, strict=True))
            row["action_label"] = ACTION_LABELS.get(row["action"], row["action"])
            row["rows"] = _detail_rows(row["detail"])
            row["created_at"] = str(row["created_at"])
            export_rows.append(row)

        # ---- 2. 报送阻断告警（设计评审 §2.2「已阻断」态） ----
        cur.execute("SELECT COUNT(*) FROM ads_report_alert WHERE alert_level='block'")
        report_alert_total = int(cur.fetchone()[0])
        cur.execute(
            "SELECT id, report_date, report_type, alert_level, check_name, detail, etl_ts "
            "FROM ads_report_alert WHERE alert_level='block' "
            "ORDER BY id DESC LIMIT %s",
            (PAGE_LIMIT,),
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

    # ---- 3. 特征归因报告（output/avm/attribution_report.json，缺失时降级） ----
    attribution = _load_json(_ATTRIBUTION_FILE)
    rel_path = os.path.relpath(_ATTRIBUTION_FILE, _REPO_ROOT)
    if attribution is None:
        attribution = {
            "available": False,
            # 降级提示给出重建命令（产物 .gitignore，克隆后需重跑训练），与 p7 的口径一致。
            "message": "归因报告未生成（permutation importance），请先运行 tools/avm 训练（产出 "
            f"<code>{rel_path}</code>）。当前展示归因概要的规则框架，待报告产出后自动填充。",
            "path": rel_path,
        }
    else:
        attribution = {"available": True, "report": attribution, "path": rel_path}

    return {
        "export_rows": export_rows,
        "export_total": export_total,
        "report_alerts": report_alerts,
        "report_alert_total": report_alert_total,
        "attribution": attribution,
    }


PAGE = {
    "id": "compliance_audit",
    "label": "合规审计 / 特征归因",
    # 合规审计明细属风控与管理员职责：贷后与 DA 不接触（PRD §7.3 语义）。
    "roles": {"admin", "risk"},
    "order": 90,
    "js": "p9_compliance_audit.js",
    "routes": {
        ("GET", "/api/compliance_audit"): get_compliance_audit,
    },
}
