"""Superset 资产导入脚本（投产加固 · L5 BI 层）。

在 Superset 已启动并完成 db upgrade / init / create-admin 后运行本脚本：
  1. 连接 Doris ADS 层（mysql+pymysql，容器内经 host.docker.internal:9030）；
  2. 注册脱敏视图数据集（仅注册视图，不注册裸明细基表，复用驾驶舱 PII 口径）：
     v_risk_class / v_avm_precision_trend / v_city_avg_price / v_compliance_audit /
     v_ltv_alerts_masked（视图定义见 sql/doris/02_superset_pii_views.sql）；
  3. 建 3 个图表 + 1 个示例仪表盘「房产金融风险概览」。

RBAC（四角色映射）走容器内 security_manager（本部署未启用 roles REST 端点）：
  docker compose exec superset python /app/setup_roles.py
见 deploy/superset/README.md §2。

运行：python deploy/superset/setup_superset.py
环境变量：SUPERSET_URL(默认 http://127.0.0.1:8088) / SUPERSET_ADMIN / SUPERSET_PASSWORD
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = os.getenv("SUPERSET_URL", "http://127.0.0.1:8088").rstrip("/")
ADMIN = os.getenv("SUPERSET_ADMIN", "admin")
PASSWORD = os.getenv("SUPERSET_PASSWORD", "superset_dev_only")

# 容器内经 host.docker.internal 访问宿主 Doris(9030)；宿主侧等价 URI 为
# mysql+pymysql://root@127.0.0.1:9030/ads（见 docker-compose.yml extra_hosts 说明）。
DORIS_URI = os.getenv(
    "SUPERSET_DORIS_URI",
    "mysql+pymysql://root@host.docker.internal:9030/ads",
)

# Superset 写操作需 CSRF token + 会话 cookie
_COOKIE_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIE_JAR))


def _req(
    method: str, path: str, token: str | None, body: dict | None = None, csrf: str | None = None
):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if csrf:
        headers["X-CSRFToken"] = csrf
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        try:
            detail = json.loads(detail)
        except Exception:
            pass
        return e.code, detail


def login() -> tuple[str, str]:
    status, body = _req(
        "POST",
        "/api/v1/security/login",
        None,
        {"username": ADMIN, "password": PASSWORD, "provider": "db", "refresh": True},
    )
    if status != 200 or "access_token" not in body:
        raise RuntimeError(f"登录失败 {status}: {body}")
    # 取 CSRF token（需带登录后写入的会话 cookie）
    s2, b2 = _req("GET", "/api/v1/security/csrf_token/", body["access_token"])
    if s2 != 200 or "result" not in b2:
        raise RuntimeError(f"取 CSRF 失败 {s2}: {b2}")
    return body["access_token"], b2["result"]


def ensure_database(token: str, csrf: str) -> int:
    name = "Doris ADS"
    status, body = _req(
        "POST",
        "/api/v1/database/",
        token,
        {
            "database_name": name,
            "sqlalchemy_uri": DORIS_URI,
            "expose_in_sqllab": True,
        },
        csrf,
    )
    if status == 201:
        return body["id"]
    # 已存在（或任意非 201）→ 拉全量按名匹配
    s2, b2 = _req("GET", "/api/v1/database/?q=(page_size:100)", token)
    if s2 == 200:
        for d in b2.get("result", []):
            if d.get("database_name") == name:
                return d["id"]
    raise RuntimeError(f"建库失败 {status}: {body}")


def ensure_dataset(token: str, csrf: str, db_id: int, table: str, schema: str = "ads") -> int:
    # 先按 table_name + schema 查重（幂等：已存在则直接复用，避免重跑累积 / 422 崩溃）
    q = urllib.parse.quote("(page_size:100)")
    s0, b0 = _req("GET", f"/api/v1/dataset/?q={q}", token)
    if s0 == 200:
        for d in b0.get("result", []):
            if d.get("table_name") == table and d.get("schema") == schema:
                return d["id"]
    # 不存在则创建
    status, body = _req(
        "POST",
        "/api/v1/dataset/",
        token,
        {
            "database": db_id,
            "table_name": table,
            "schema": schema,
        },
        csrf,
    )
    if status == 201:
        return body["id"]
    if status == 422:
        # 竞态：查询至创建之间被他人创建 → 重新查一次按 table_name + schema 匹配
        s1, b1 = _req("GET", f"/api/v1/dataset/?q={q}", token)
        for d in b1.get("result", []):
            if d.get("table_name") == table and d.get("schema") == schema:
                return d["id"]
    raise RuntimeError(f"建数据集 {table} 失败 {status}: {body}")


def create_chart(token: str, csrf: str, name: str, viz_type: str, ds_id: int, params: dict) -> int:
    params = {"datasource": f"{ds_id}__table", "viz_type": viz_type, "slice_id": 0, **params}
    status, body = _req(
        "POST",
        "/api/v1/chart/",
        token,
        {
            "slice_name": name,
            "viz_type": viz_type,
            "datasource_id": ds_id,
            "datasource_type": "table",
            "params": json.dumps(params),
        },
        csrf,
    )
    if status != 201:
        raise RuntimeError(f"建图表 {name} 失败 {status}: {body}")
    return body["id"]


def create_dashboard(token: str, csrf: str, title: str, chart_ids: list[int]) -> int:
    status, body = _req(
        "POST",
        "/api/v1/dashboard/",
        token,
        {
            "dashboard_title": title,
            "slices": chart_ids,
        },
        csrf,
    )
    if status != 201:
        s2, b2 = _req("POST", "/api/v1/dashboard/", token, {"dashboard_title": title}, csrf)
        if s2 == 201:
            return b2["id"]
        raise RuntimeError(f"建仪表盘失败 {status}: {body}")
    return body["id"]


def main() -> int:
    token, csrf = login()
    print("[1/4] 已登录 Superset")

    db_id = ensure_database(token, csrf)
    print(f"[2/4] Doris 数据库连接 id={db_id} ({DORIS_URI})")

    rc = ensure_dataset(token, csrf, db_id, "v_risk_class")
    avm = ensure_dataset(token, csrf, db_id, "v_avm_precision_trend")
    cap = ensure_dataset(token, csrf, db_id, "v_city_avg_price")
    # 受限/脱敏视图也注册为数据集，但 RBAC（setup_roles.py）仅对 admin/risk 等授权可见。
    ensure_dataset(token, csrf, db_id, "v_compliance_audit")
    ensure_dataset(token, csrf, db_id, "v_ltv_alerts_masked", schema="ods")
    print(
        f"[3/4] 数据集(视图): v_risk_class={rc} v_avm_precision_trend={avm} v_city_avg_price={cap}"
        " + 受限 v_compliance_audit / v_ltv_alerts_masked(ods)"
    )

    c1 = create_chart(
        token,
        csrf,
        "资产质量概览(总余额)",
        "big_number_total",
        rc,
        {
            "metric": "sum__balance_total",
            "row_limit": 1,
        },
    )
    c2 = create_chart(
        token,
        csrf,
        "五级分类分布",
        "pie",
        rc,
        {
            "groupby": ["risk_class"],
            "metric": "sum__balance_total",
            "row_limit": 5000,
            "label_type": "key_value",
            "donut": True,
        },
    )
    c3 = create_chart(
        token,
        csrf,
        "AVM 精度趋势",
        "echarts_timeseries_line",
        avm,
        {
            "metrics": ["model_mape", "baseline_mape"],
            "x_axis": "stat_date",
            "row_limit": 1000,
            "x_axis_format": "smart_date",
        },
    )
    print(f"[4/4] 图表: 资产质量={c1} 五级分类={c2} AVM趋势={c3}")

    dash = create_dashboard(token, csrf, "房产金融风险概览", [c1, c2, c3])
    print(f"仪表盘「房产金融风险概览」id={dash} 已关联 {len([c1, c2, c3])} 个图表")

    print("\n✅ Superset 资产导入完成。打开 http://127.0.0.1:8088 用 admin 登录查看。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
