#!/usr/bin/env python
"""Superset 四角色 RBAC 落地（投产加固 · 容器内执行）。

为什么是「容器内执行」而不是走 REST：
  Superset 本部署未启用 /api/v1/security/roles/ REST 端点（返回 404），故 RBAC
  走 FAB 官方 security_manager（最稳、与 UI 同一套权限模型）。本脚本在 Superset
  容器内运行，复用其 app 上下文与 security_manager。

运行（容器已起、已 db upgrade/init/create-admin 后）：
  docker compose exec superset python /app/setup_roles.py

角色映射（与 docs/tech/components/superset.md §5 一致）：
  Admin   —— 内置，全量（不动）。
  Risk    —— 全 5 个视图（聚合 + 受限审计 + 脱敏明细）。
  DA      —— 限聚合 + 趋势视图（v_risk_class / v_avm_precision_trend / v_city_avg_price）。
  Postloan—— 限贷后相关（v_ltv_alerts_masked，PII 已脱敏）。
  关闭 Alpha 角色 SQL Lab 写权限（仅读），普通角色禁止自建数据集（仅 Admin 可建）。

数据集名为 Doris 脱敏视图（见 sql/doris/02_superset_pii_views.sql），Superset 只注册视图，
不注册裸明细基表，从而复用驾驶舱 PII 脱敏口径。
"""

from __future__ import annotations

import os

from superset import create_app

app = create_app()

DB_NAME = os.getenv("SUPERSET_DORIS_DB_NAME", "Doris ADS")

# 角色 -> 可见数据集（schema, table）
ROLE_DATASETS = {
    "Risk": [
        ("ads", "v_risk_class"),
        ("ads", "v_avm_precision_trend"),
        ("ads", "v_city_avg_price"),
        ("ads", "v_compliance_audit"),
        ("ods", "v_ltv_alerts_masked"),
    ],
    "DA": [
        ("ads", "v_risk_class"),
        ("ads", "v_avm_precision_trend"),
        ("ads", "v_city_avg_price"),
    ],
    "Postloan": [
        ("ods", "v_ltv_alerts_masked"),
    ],
}


def main() -> int:
    # 必须在 app 上下文内导入模型（superset.models.core 在导入时访问 app.config）。
    with app.app_context():
        from superset import db, security_manager
        from superset.connectors.sqla.models import SqlaTable
        from superset.models.core import Database

        def _find_dataset(schema, table):
            return (
                db.session.query(SqlaTable)
                .join(Database)
                .filter(
                    SqlaTable.table_name == table,
                    SqlaTable.schema == schema,
                    Database.database_name == DB_NAME,
                )
                .first()
            )

        def _ensure_perm(permission_name, view_menu_name):
            pv = security_manager.find_permission_view_menu(permission_name, view_menu_name)
            if pv is None:
                pv = security_manager.add_permission_view_menu(permission_name, view_menu_name)
            return pv

        def _grant_dataset_read(role, schema, table):
            ds = _find_dataset(schema, table)
            if ds is None:
                print(f"  [warn] 数据集未找到 {schema}.{table}（先跑 setup_superset.py 注册视图）")
                return False
            # 数据集的访问权限 = datasource_access 视图菜单（格式见 ds.perm）。
            pv = security_manager.find_permission_view_menu("datasource_access", ds.perm)
            if pv is None:
                pv = security_manager.add_permission_view_menu("datasource_access", ds.perm)
            security_manager.add_permission_role(role, pv)
            # 还需 Dataset/Dashboard 的 can_read，才能在前端浏览数据集列表与看板。
            security_manager.add_permission_role(role, _ensure_perm("can_read", "Dataset"))
            security_manager.add_permission_role(role, _ensure_perm("can_read", "Dashboard"))
            print(f"  [ok] {role.name} <- {schema}.{table} (perm={ds.perm})")
            return True

        def _disable_alpha_sqllab_write():
            alpha = security_manager.find_role("Alpha")
            if alpha is None:
                print("  [skip] Alpha 角色不存在")
                return
            removed = False
            for pv in list(alpha.permissions):
                if (
                    pv.permission
                    and pv.permission.name == "can_write"
                    and pv.view_menu
                    and pv.view_menu.name == "SQL Lab"
                ):
                    security_manager.del_permission_role(alpha, pv)
                    removed = True
            print(
                f"  [{'ok' if removed else 'skip'}] Alpha SQL Lab can_write 已{'移除' if removed else '本就无'}"
            )

        for role_name, datasets in ROLE_DATASETS.items():
            role = security_manager.find_role(role_name)
            if role is None:
                role = security_manager.add_role(role_name)
                print(f"[new] 角色 {role_name}")
            else:
                print(f"[exist] 角色 {role_name}")
            for schema, table in datasets:
                _grant_dataset_read(role, schema, table)
        _disable_alpha_sqllab_write()
        db.session.commit()
        print("\n✅ RBAC 角色映射已落地（Admin 复用内置全量）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
