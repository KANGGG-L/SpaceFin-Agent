#!/usr/bin/env python
"""P6 空间惩罚项配置（US-01 / 设计评审 §1 P6）。

用户故事：作为风控策略经理，我希望配置区域空间惩罚项（如对落入特定高危网格的贷款
自动加严审批或拒贷），以便在审批环节就把空间风险纳入决策。

本页是全平台唯一的「写操作 + 策略配置」页面，设计上有三条硬约束：

1. **先看后果再落库**（本页的灵魂）。策略配置最怕「拍脑袋压阈值」——把某城 LTV 上限
   从 0.85 压到 0.70，究竟波及多少存量、多少余额、多少笔从「通过」翻成「拒贷」，
   风控经理在保存前必须看得见。因此写接口与预览接口共用同一套命中算法
   （`_impact()`），页面上「预览」与「保存后返回的影响」口径逐字一致，
   这就是 PRD §5.3 策略沙盒（P1）在 P0 页面上的雏形。

2. **RBAC 双层校验**。页面 roles 含 da（PRD §7.3：DA 只读），但写操作在 handler 内
   二次校验 `role in WRITE_ROLES`，否则 403。只靠页面级 roles 会把 DA 放进来写，
   只靠前端隐藏按钮更是形同虚设。

3. **写操作必须留痕**（R-UNW-02 语义延伸）。谁、什么时候、把哪个区域的 LTV 上限
   改成了多少、命中多少笔——全部落 ads_export_audit，成功失败都记。

口径对齐：LTV 红线、五级分类边界一律取 tools/risk/config，不在本文件另立阈值；
网格（0.02°）与 5km 邻域半径口径同 tools/spatial/config（见 ZONE_MATCH_RADIUS_KM）。
"""

import json
import math
import os
import sys

# app.py 已把 tools/frontend 放进 sys.path；此处再插一次是为了「单独 import pages」
# 做冒烟检查时（验收脚本里就是这么跑的）也能解析到同级的 db 模块。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402

# 直接借用 db 已经加载好的 tools/risk/config（db 导入时把 tools/risk 挂进了 sys.path）。
# 复用而不复制：LTV 红线 0.85 与五级边界一旦两处各写一份，页面预览的「现状」
# 就会和风险引擎实际跑出来的 dws_risk_class 对不上。
config = db.config

# ---------------- 口径常量 ----------------

# 写权限：PRD §7.3「风控策略经理读写策略配置；DA 只读」。admin 作为系统管理员一并放行。
WRITE_ROLES = {"admin", "risk"}

# 惩罚动作三档。语义按设计评审 P6「加严审批 / 拒贷规则」，补一档「仅提示」用于灰度观察。
ACTIONS = {
    "reject": "拒贷",
    "strict": "加严审批",
    "notice": "仅提示",
}

# 规则粒度：网格（ads_spatial_zone.zone_id）/ 城市（config.CITY_MAP 的城市码）。
SCOPES = ("zone", "city")

# 网格命中半径（km）。为什么不用「坐标严格落在 0.02° 网格内」：
# 网格边长约 2.2km，而抵押物坐标是合成/回填值（见 tools/spatial 的 coord 说明），
# 严格落格只有 2/200 笔命中，预览失去意义。改用「到网格中心 ≤ 5km」，
# 5km 即 tools/spatial/config.NEIGHBOR_RADIUS_KM——本项目「局部邻域」的既有口径。
ZONE_MATCH_RADIUS_KM = 5.0

# 高危区建议规则的默认 LTV 上限：红线 0.85 之下再压一档，落到「关注」级上界 0.75 以下。
SUGGEST_LTV_CAP = 0.70

# 城市码 → 中文名（config.CITY_MAP 是反向的）。
CITY_NAME = {code: name for name, code in config.CITY_MAP.items()}

TABLE = "ads_spatial_policy"


# ---------------- 建表 ----------------


def ensure_policy_table():
    """幂等建策略表（root + 房产库）。

    为什么用 ddl_conn：app 账号只有 DML 没有 DDL 权限（同 db.ensure_alert_confirm_table）。
    为什么 (scope_type, scope_value) 唯一：同一个网格/城市只允许一条生效规则，
    否则「该区域 LTV 上限到底是多少」会出现两条互相打架的答案，审批侧无法裁决。
    """
    conn = db.ddl_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE} ("
            "policy_id INT AUTO_INCREMENT PRIMARY KEY,"
            "scope_type VARCHAR(8) NOT NULL,"
            "scope_value VARCHAR(64) NOT NULL,"
            "action VARCHAR(16) NOT NULL,"
            "ltv_cap DECIMAL(5,4) NULL,"
            "reason VARCHAR(255),"
            "enabled TINYINT NOT NULL DEFAULT 1,"
            "created_by VARCHAR(32) NOT NULL,"
            "updated_by VARCHAR(32),"
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
            "UNIQUE KEY uq_scope (scope_type, scope_value),"
            "KEY idx_enabled (enabled)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


# ---------------- 存量快照（命中计算的输入） ----------------


def _haversine_km(lat1, lng1, lat2, lng2):
    """球面距离（km）。抵押物点到网格中心的距离判定用，量级 km，用球面公式足够。"""
    r = 6371.0
    p = math.pi / 180.0
    a = (
        math.sin((lat2 - lat1) * p / 2) ** 2
        + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def _city_from_addr(addr):
    """「广州市黄埔区…」→ 城市码 gz。取不到返回 None（该笔不参与城市粒度规则）。"""
    if not addr:
        return None
    for name, code in config.CITY_MAP.items():
        if str(addr).startswith(name):
            return code
    return None


def _load_snapshot():
    """把存量贷款 + 抵押物坐标 + 地址城市 + 网格画像一次性读进内存。

    为什么全量进内存而不在 SQL 里算：明细只有 200 行级；命中判定要做球面距离，
    在 MySQL 里拼 haversine 既难读又难复用，而同一次预览请求要按「区域内 / 命中 /
    新增命中 / 分五级」反复聚合，进内存后一次读、多次算，比多轮往返数据库更清晰。
    """
    crawl = db.crawl_conn()
    biz = db.biz_conn()
    try:
        cur = crawl.cursor()
        cur.execute(
            "SELECT loan_id, collateral_id, balance, ltv, risk_class, "
            "low_confidence, is_high_risk_zone FROM dws_risk_class"
        )
        loans = [
            {
                "loan_id": int(r[0]),
                "collateral_id": int(r[1]) if r[1] is not None else None,
                "balance": float(r[2] or 0),
                "ltv": float(r[3]) if r[3] is not None else None,
                "risk_class": r[4],
                "low_confidence": int(r[5] or 0),
                "is_high_risk_zone": int(r[6] or 0),
            }
            for r in cur.fetchall()
        ]

        # 抵押物坐标：DWS 空间特征层（collateral 实体），是全仓「抵押物落点」的唯一口径。
        cur.execute(
            "SELECT entity_id, lat, lng FROM dws_spatial_feature WHERE entity_type='collateral'"
        )
        coords = {}
        for eid, lat, lng in cur.fetchall():
            try:
                coords[int(eid)] = (
                    float(lat) if lat is not None else None,
                    float(lng) if lng is not None else None,
                )
            except (TypeError, ValueError):
                continue

        cur.execute(
            "SELECT zone_id, zone_type, city, center_lat, center_lng, sample_count, "
            "median_unit_price, median_ltv, price_dev_vs_city, is_high_risk_zone, high_risk_rule "
            "FROM ads_spatial_zone"
        )
        zones = {}
        for r in cur.fetchall():
            zones[r[0]] = {
                "zone_id": r[0],
                "zone_type": r[1],
                "city": r[2],
                "center_lat": float(r[3]) if r[3] is not None else None,
                "center_lng": float(r[4]) if r[4] is not None else None,
                "sample_count": int(r[5] or 0),
                "median_unit_price": float(r[6]) if r[6] is not None else None,
                "median_ltv": float(r[7]) if r[7] is not None else None,
                "price_dev_vs_city": float(r[8]) if r[8] is not None else None,
                "is_high_risk_zone": int(r[9] or 0),
                "high_risk_rule": r[10],
            }
        cur.close()

        # 城市只能从业务库抵押物地址解析：dws_spatial_feature.district 对 collateral 实体全为 NULL
        # （与 db.dashboard 的城市口径保持一致，页面之间不能出现两种城市归属）。
        bcur = biz.cursor()
        bcur.execute("SELECT collateral_id, property_addr FROM collateral")
        addrs = {int(r[0]): r[1] for r in bcur.fetchall()}
        bcur.close()
    finally:
        crawl.close()
        biz.close()

    for loan in loans:
        cid = loan["collateral_id"]
        lat, lng = coords.get(cid, (None, None))
        loan["lat"] = lat
        loan["lng"] = lng
        loan["addr"] = addrs.get(cid)
        loan["city"] = _city_from_addr(loan["addr"])

    return {"loans": loans, "zones": zones}


def _in_scope(loan, scope_type, scope_value, zones):
    """判定一笔贷款是否落在规则作用域内。"""
    if scope_type == "city":
        return loan["city"] == scope_value
    zone = zones.get(scope_value)
    if not zone or zone["center_lat"] is None or loan["lat"] is None:
        return False
    dist = _haversine_km(loan["lat"], loan["lng"], zone["center_lat"], zone["center_lng"])
    return dist <= ZONE_MATCH_RADIUS_KM


def scope_label(scope_type, scope_value, zones):
    """作用域的人话标签，前端与审计明细共用，避免两处措辞漂移。"""
    if scope_type == "city":
        return f"城市 · {CITY_NAME.get(scope_value, scope_value)}"
    zone = zones.get(scope_value) if zones else None
    if zone and zone["city"]:
        return f"网格 · {CITY_NAME.get(zone['city'], zone['city'])} {scope_value}"
    return f"网格 · {scope_value}"


# ---------------- 命中影响预览（本页核心） ----------------


def _bucket():
    return {"loans": 0, "balance": 0.0}


def _add(bucket, loan):
    bucket["loans"] += 1
    bucket["balance"] += loan["balance"]


def _round(bucket):
    return {"loans": bucket["loans"], "balance": round(bucket["balance"], 2)}


def _impact(snapshot, scope_type, scope_value, action, ltv_cap):
    """算一条规则落地后的存量影响。预览与保存共用，保证「看到的」= 「存下的」。

    口径定义（写清楚，否则数字没人敢信）：
    - 区域内（in_scope）：抵押物落在该网格 5km 内 / 地址属于该城市的全部存量贷款；
    - 命中（hit）：区域内且 LTV > 本规则的 ltv_cap（未设 ltv_cap 时视为整区命中）；
    - 现状已预警（already_alert）：命中笔中 LTV 已经超过全局红线（config.LTV_RED_LINE，
      默认 0.85）的部分——这些笔在现有链路里本来就会进 LTV 预警，规则只是提高了处置等级；
    - **新增命中（newly_hit）**：命中笔中 LTV ≤ 全局红线的部分。这才是「原本能通过、
      规则生效后被加严/拒贷」的增量，是风控经理拍板的关键数字；
    - 低置信（low_confidence）：命中笔中 R-UNW-01 标记的低置信估值。这些笔按 PRD 降级
      策略应回退人工复核，不建议直接自动拒贷，所以单列出来提醒。
    """
    red = config.LTV_RED_LINE
    loans = snapshot["loans"]
    zones = snapshot["zones"]

    in_scope, hit, newly_hit, already_alert = _bucket(), _bucket(), _bucket(), _bucket()
    low_conf = 0
    by_class = {}
    rows = []

    for loan in loans:
        if not _in_scope(loan, scope_type, scope_value, zones):
            continue
        _add(in_scope, loan)
        ltv = loan["ltv"]
        # ltv 缺失按保守口径处理：不判命中（风险引擎对缺失 LTV 已归「次级」并单独走人工），
        # 这里若强行判命中会把「没估值」误报成「被新规则拒掉」，虚增增量。
        matched = ltv is not None and (ltv_cap is None or ltv > ltv_cap)
        if not matched:
            continue
        _add(hit, loan)
        low_conf += loan["low_confidence"]
        cls = by_class.setdefault(loan["risk_class"], _bucket())
        _add(cls, loan)
        if ltv > red:
            _add(already_alert, loan)
            change = "已预警→提级处置"
        else:
            _add(newly_hit, loan)
            change = "通过→" + ACTIONS.get(action, action)
        rows.append(
            {
                "loan_id": loan["loan_id"],
                "collateral_id": loan["collateral_id"],
                "ltv": round(ltv, 4),
                "balance": round(loan["balance"], 2),
                "risk_class": loan["risk_class"],
                "low_confidence": loan["low_confidence"],
                "is_high_risk_zone": loan["is_high_risk_zone"],
                "addr": loan["addr"] or "未标注",
                "change": change,
            }
        )

    # 命中明细按 LTV 从高到低——风控看的第一眼永远是最危险的那几笔。
    rows.sort(key=lambda r: r["ltv"], reverse=True)
    ordered_class = [
        {"risk_class": c, **_round(by_class[c])} for c in config.CLASS_ORDER if c in by_class
    ]
    hit_balance_pct = (hit["balance"] / in_scope["balance"]) if in_scope["balance"] > 0 else 0.0

    return {
        "scope_type": scope_type,
        "scope_value": scope_value,
        "scope_label": scope_label(scope_type, scope_value, zones),
        "action": action,
        "action_label": ACTIONS.get(action, action),
        "ltv_cap": ltv_cap,
        "ltv_red_line": red,
        "in_scope": _round(in_scope),
        "hit": _round(hit),
        "newly_hit": _round(newly_hit),
        "already_alert": _round(already_alert),
        "low_confidence": low_conf,
        "hit_balance_pct": round(hit_balance_pct, 4),
        "by_class": ordered_class,
        "rows": rows[:50],
        "rows_truncated": max(0, len(rows) - 50),
    }


# ---------------- 参数校验 ----------------


def _valid_scope(scope_type, scope_value, zones):
    """作用域必须指向真实存在的网格/城市，避免规则悬空在一个不存在的区域上。"""
    if scope_type not in SCOPES:
        raise ValueError(f"scope_type 非法，仅支持 {'/'.join(SCOPES)}")
    if not scope_value:
        raise ValueError("scope_value 不能为空")
    if scope_type == "zone" and scope_value not in zones:
        raise ValueError(f"网格不存在：{scope_value}")
    if scope_type == "city" and scope_value not in CITY_NAME:
        raise ValueError(f"城市码不存在：{scope_value}")


def _parse_rule(body, zones):
    """解析并校验一条规则的入参，返回规范化后的字段。非法一律 ValueError（app.py 转 400）。"""
    scope_type = (body.get("scope_type") or "").strip()
    scope_value = (body.get("scope_value") or "").strip()
    _valid_scope(scope_type, scope_value, zones)

    action = (body.get("action") or "").strip()
    if action not in ACTIONS:
        raise ValueError(f"action 非法，仅支持 {'/'.join(ACTIONS)}")

    raw_cap = body.get("ltv_cap")
    ltv_cap = None
    if raw_cap not in (None, ""):
        try:
            ltv_cap = float(raw_cap)
        except (TypeError, ValueError):
            raise ValueError("ltv_cap 必须是数字") from None
        # 上限 2.0：LTV 理论上可以 >1（资不抵债），但 >2 基本是填错了小数点。
        if not 0 < ltv_cap <= 2:
            raise ValueError("ltv_cap 必须落在 (0, 2] 区间")
        ltv_cap = round(ltv_cap, 4)
    if action in ("reject", "strict") and ltv_cap is None:
        # 拒贷/加严必须有可判定的门槛，否则审批侧不知道「超过多少才拒」。
        raise ValueError("拒贷 / 加严审批规则必须填写 ltv_cap")

    reason = (body.get("reason") or "").strip()[:255]
    enabled = 1 if body.get("enabled", True) else 0
    return scope_type, scope_value, action, ltv_cap, reason, enabled


def _audit(ctx, action_name, detail, result):
    """写操作留痕。ip 由框架经 RouteCtx 下发（R-UNW-02 要求审计可追溯到来源）。"""
    db.write_audit(
        action_name,
        ctx.user["user"],
        ctx.user["role"],
        json.dumps(detail, ensure_ascii=False),
        result,
        getattr(ctx, "ip", "-"),
    )


def _deny(ctx, action_name, detail):
    """非写角色的拒绝路径：先留痕再回 403——越权尝试本身就是要审计的事件。"""
    _audit(ctx, action_name, {**detail, "denied_role": ctx.user["role"]}, "denied")
    return 403, {"error": "无权限：仅风控策略经理（risk）/ 管理员（admin）可修改空间惩罚项"}


# ---------------- 读接口 ----------------


def _list_policies():
    conn = db.crawl_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT policy_id, scope_type, scope_value, action, ltv_cap, reason, enabled, "
            f"created_by, updated_by, created_at, updated_at FROM {TABLE} "
            "ORDER BY enabled DESC, updated_at DESC, policy_id DESC"
        )
        out = []
        for r in cur.fetchall():
            out.append(
                {
                    "policy_id": int(r[0]),
                    "scope_type": r[1],
                    "scope_value": r[2],
                    "action": r[3],
                    "action_label": ACTIONS.get(r[3], r[3]),
                    "ltv_cap": float(r[4]) if r[4] is not None else None,
                    "reason": r[5],
                    "enabled": int(r[6]),
                    "created_by": r[7],
                    "updated_by": r[8],
                    "created_at": str(r[9]) if r[9] else None,
                    "updated_at": str(r[10]) if r[10] else None,
                }
            )
        cur.close()
        return out
    finally:
        conn.close()


def handle_list(ctx):
    """GET /api/policy —— 规则列表（含每条规则当前命中量）+ 网格/城市候选 + 全局阈值。"""
    ensure_policy_table()
    snapshot = _load_snapshot()
    zones = snapshot["zones"]

    policies = _list_policies()
    for p in policies:
        imp = _impact(snapshot, p["scope_type"], p["scope_value"], p["action"], p["ltv_cap"])
        p["scope_label"] = imp["scope_label"]
        # 列表页只带汇总，明细留给预览接口，避免一次返回上千行。
        p["impact"] = {
            "in_scope": imp["in_scope"],
            "hit": imp["hit"],
            "newly_hit": imp["newly_hit"],
            "low_confidence": imp["low_confidence"],
        }

    # 网格候选：带上「区域内存量贷款数」，让风控一眼看出哪些网格配了才有意义。
    zone_cover = {zid: _bucket() for zid in zones}
    city_cover = {}
    for loan in snapshot["loans"]:
        for zid, zone in zones.items():
            if zone["center_lat"] is None or loan["lat"] is None:
                continue
            if (
                _haversine_km(loan["lat"], loan["lng"], zone["center_lat"], zone["center_lng"])
                <= ZONE_MATCH_RADIUS_KM
            ):
                _add(zone_cover[zid], loan)
        if loan["city"]:
            _add(city_cover.setdefault(loan["city"], _bucket()), loan)

    zone_rows = [
        {
            **{k: v for k, v in z.items() if k not in ("center_lat", "center_lng")},
            "city_name": CITY_NAME.get(z["city"], z["city"]),
            "cover": _round(zone_cover[zid]),
        }
        for zid, z in zones.items()
    ]
    # 高危区在前、覆盖存量多的在前——「一键生成建议」要用的就是这个顺序。
    zone_rows.sort(key=lambda z: (-z["is_high_risk_zone"], -z["cover"]["loans"], z["zone_id"]))

    city_rows = sorted(
        (
            {"city": code, "city_name": CITY_NAME.get(code, code), "cover": _round(cov)}
            for code, cov in city_cover.items()
        ),
        key=lambda c: -c["cover"]["loans"],
    )

    return {
        "policies": policies,
        "zones": zone_rows,
        "cities": city_rows,
        "actions": [{"value": k, "label": v} for k, v in ACTIONS.items()],
        "can_write": ctx.user["role"] in WRITE_ROLES,
        "thresholds": {
            "ltv_red_line": config.LTV_RED_LINE,
            "class_ltv_upper": config.CLASS_LTV_UPPER,
            "zone_match_radius_km": ZONE_MATCH_RADIUS_KM,
            "suggest_ltv_cap": SUGGEST_LTV_CAP,
        },
    }


def handle_preview(ctx):
    """POST /api/policy/preview —— 只读试算：这条规则若生效，存量会怎样。

    刻意不限制写角色：DA 需要能试算（PRD §7.3 DA 只读但要能分析），
    而预览不产生任何 DB 变更，也就不构成越权。
    """
    ensure_policy_table()
    snapshot = _load_snapshot()
    scope_type, scope_value, action, ltv_cap, _reason, _enabled = _parse_rule(
        ctx.body, snapshot["zones"]
    )
    return _impact(snapshot, scope_type, scope_value, action, ltv_cap)


def handle_suggest(ctx):
    """GET /api/policy/suggest —— 为 is_high_risk_zone=1 的网格生成建议规则草案。

    只出草案不落库：批量写策略是高风险动作，必须由风控经理逐条（或一键）确认后
    走 POST /api/policy 的正规写入路径，这样每条规则都有独立的审计记录和 created_by。
    """
    ensure_policy_table()
    snapshot = _load_snapshot()
    zones = snapshot["zones"]
    existing = {(p["scope_type"], p["scope_value"]) for p in _list_policies()}

    drafts = []
    for zid, z in zones.items():
        if not z["is_high_risk_zone"]:
            continue
        # 建议动作按高危成因分档：价格洼地（抵押物变现折价风险）先加严观察；
        # LTV 集中型（整片区已经普遍高杠杆）直接建议拒贷。
        action = "reject" if z["high_risk_rule"] == "ltv_high" else "strict"
        reason = (
            f"高危网格自动建议（规则 {z['high_risk_rule']}，样本 {z['sample_count']}）："
            f"LTV 上限压至 {SUGGEST_LTV_CAP:.2f}"
        )
        imp = _impact(snapshot, "zone", zid, action, SUGGEST_LTV_CAP)
        drafts.append(
            {
                "scope_type": "zone",
                "scope_value": zid,
                "scope_label": imp["scope_label"],
                "action": action,
                "action_label": ACTIONS[action],
                "ltv_cap": SUGGEST_LTV_CAP,
                "reason": reason,
                "high_risk_rule": z["high_risk_rule"],
                "price_dev_vs_city": z["price_dev_vs_city"],
                "median_ltv": z["median_ltv"],
                "sample_count": z["sample_count"],
                "exists": ("zone", zid) in existing,
                "impact": {
                    "in_scope": imp["in_scope"],
                    "hit": imp["hit"],
                    "newly_hit": imp["newly_hit"],
                    "low_confidence": imp["low_confidence"],
                },
            }
        )
    # 有存量命中的排前面：没有任何贷款落在里面的高危网格，配了也只是占位。
    drafts.sort(key=lambda d: (-d["impact"]["hit"]["loans"], -d["impact"]["in_scope"]["loans"]))
    return {"drafts": drafts, "suggest_ltv_cap": SUGGEST_LTV_CAP, "total": len(drafts)}


# ---------------- 写接口（仅 admin / risk） ----------------


def handle_save(ctx):
    """POST /api/policy —— 新建或更新一条空间惩罚项。

    幂等语义：同一 (scope_type, scope_value) 上重复保存 = 覆盖更新（UNIQUE + ON DUPLICATE），
    与 db.confirm_alert 的做法一致；页面上「编辑」和「一键采纳建议」都走这一条路径。
    """
    if ctx.user["role"] not in WRITE_ROLES:
        return _deny(ctx, "policy_save", {"body": ctx.body})

    ensure_policy_table()
    snapshot = _load_snapshot()
    try:
        scope_type, scope_value, action, ltv_cap, reason, enabled = _parse_rule(
            ctx.body, snapshot["zones"]
        )
    except ValueError as exc:
        # 参数不合法也留痕：越权与误操作同样需要可追溯（TC-06）。
        _audit(ctx, "policy_save", {"body": ctx.body, "error": str(exc)}, "failure")
        raise

    imp = _impact(snapshot, scope_type, scope_value, action, ltv_cap)

    conn = db.crawl_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO {TABLE} (scope_type, scope_value, action, ltv_cap, reason, enabled, "
            "created_by, updated_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE action=VALUES(action), ltv_cap=VALUES(ltv_cap), "
            "reason=VALUES(reason), enabled=VALUES(enabled), updated_by=VALUES(updated_by)",
            (
                scope_type,
                scope_value,
                action,
                ltv_cap,
                reason,
                enabled,
                ctx.user["user"],
                ctx.user["user"],
            ),
        )
        conn.commit()
        cur.execute(
            f"SELECT policy_id FROM {TABLE} WHERE scope_type=%s AND scope_value=%s",
            (scope_type, scope_value),
        )
        row = cur.fetchone()
        policy_id = int(row[0]) if row else None
        cur.close()
    finally:
        conn.close()

    # 审计明细带上命中量：事后追责时能还原「当时他是看着这个数字按下保存的」。
    _audit(
        ctx,
        "policy_save",
        {
            "policy_id": policy_id,
            "scope": f"{scope_type}:{scope_value}",
            "action": action,
            "ltv_cap": ltv_cap,
            "enabled": enabled,
            "reason": reason,
            "hit_loans": imp["hit"]["loans"],
            "hit_balance": imp["hit"]["balance"],
            "newly_hit_loans": imp["newly_hit"]["loans"],
        },
        "success",
    )
    return {"ok": True, "policy_id": policy_id, "impact": imp}


def handle_delete(ctx):
    """POST /api/policy/delete —— 删除一条规则（按 policy_id）。"""
    if ctx.user["role"] not in WRITE_ROLES:
        return _deny(ctx, "policy_delete", {"body": ctx.body})

    ensure_policy_table()
    raw = ctx.body.get("policy_id")
    try:
        policy_id = int(raw)
    except (TypeError, ValueError):
        _audit(ctx, "policy_delete", {"policy_id": raw, "error": "policy_id 非法"}, "failure")
        raise ValueError("policy_id 必须为整数") from None

    conn = db.crawl_conn()
    try:
        cur = conn.cursor()
        # 先查再删：审计明细要记下删掉的是哪条规则，删完就查不到了。
        cur.execute(
            f"SELECT scope_type, scope_value, action, ltv_cap FROM {TABLE} WHERE policy_id=%s",
            (policy_id,),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            _audit(ctx, "policy_delete", {"policy_id": policy_id, "error": "不存在"}, "failure")
            return 404, {"error": f"规则不存在：policy_id={policy_id}"}
        cur.execute(f"DELETE FROM {TABLE} WHERE policy_id=%s", (policy_id,))
        conn.commit()
        cur.close()
    finally:
        conn.close()

    _audit(
        ctx,
        "policy_delete",
        {
            "policy_id": policy_id,
            "scope": f"{row[0]}:{row[1]}",
            "action": row[2],
            "ltv_cap": float(row[3]) if row[3] is not None else None,
        },
        "success",
    )
    return {"ok": True, "policy_id": policy_id}


PAGE = {
    "id": "policy",
    "label": "空间惩罚项配置",
    # da 可见但不可写（写权限在各 handler 内按 WRITE_ROLES 二次校验）；
    # postloan 不在列表内——PRD §7.3「贷后资产保全不可见策略配置」。
    "roles": {"admin", "risk", "da"},
    "order": 60,
    "js": "p6_policy.js",
    "routes": {
        ("GET", "/api/policy"): handle_list,
        ("GET", "/api/policy/suggest"): handle_suggest,
        ("POST", "/api/policy"): handle_save,
        ("POST", "/api/policy/preview"): handle_preview,
        ("POST", "/api/policy/delete"): handle_delete,
    },
}
