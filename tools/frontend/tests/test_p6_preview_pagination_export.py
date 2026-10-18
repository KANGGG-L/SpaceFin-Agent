"""增强一·后端验收：命中明细分页 / 总量 / PII / handle_preview 参数透传（spec §1e）。

零 DB：直接调 `_impact` 喂内存快照（仿 test_p6_policy_scenario 玩法）；仅测
`handle_preview` 的 page/page_size 透传时 monkeypatch `_load_snapshot`。
覆盖 §1e 1/2/3/4/5/6：默认向后兼容、60 笔分页、整页取全量、非法值 ValueError、
PII 红线（无 customer_id/customer_name）、handle_preview 参数解析与非法值。

R2-7 之后 `handle_preview` 会顺带写一条 policy_preview 审计——本文件测的是分页透传，
不测留痕，故用下面的 autouse fixture 把它空转掉：留痕的判据全部在
test_p6_preview_history.py（那边把 db.write_audit 换成录制 stub）。
不这么做的话，跑一次本文件就往真库塞 2 行审计（第一版实测确实塞了，id 76/77）。
"""

import json

import pages.p6_policy as p6
import pytest

ZONE_ID = "price-gz-113.30-23.14"


@pytest.fixture(autouse=True)
def _no_preview_audit(monkeypatch):
    """本文件的 handle_preview 用例不落审计（审计行为由 test_p6_preview_history.py 专测）。"""
    monkeypatch.setattr(p6, "_audit_preview", lambda ctx, detail: None)


def _snapshot(n):
    """造 n 笔全部落在作用域内且 LTV > cap 的存量贷款，全部命中。"""
    zones = {
        ZONE_ID: {
            "zone_id": ZONE_ID,
            "zone_type": "price",
            "city": "gz",
            "center_lat": 23.14,
            "center_lng": 113.30,
            "sample_count": n,
            "median_unit_price": 30000.0,
            "median_ltv": 0.7,
            "price_dev_vs_city": -0.1,
            "is_high_risk_zone": 1,
            "high_risk_rule": "price_low",
        }
    }
    loans = []
    for i in range(1, n + 1):
        loans.append(
            {
                "loan_id": i,
                "collateral_id": i,
                "balance": 1_000_000.0,
                "ltv": round(0.70 + 0.001 * i, 4),  # 全部 > cap 0.70 → 全中
                "risk_class": "关注",
                "low_confidence": 0,
                "is_high_risk_zone": 0,
                "lat": 23.14,
                "lng": 113.30,
                "addr": f"广州市黄埔区某路{i}号",
                "city": "gz",
            }
        )
    return {"loans": loans, "zones": zones}


def _reduce(snap, monkeypatch, **kwargs):
    """路线 B 之后分页切片下移到 handle_preview，故分页/总量判据改在端点层验证。"""
    monkeypatch.setattr(p6, "_load_snapshot", lambda: snap)
    user = {"user": "admin", "role": "admin", "label": "系统管理员"}
    ctx = type(
        "_Ctx",
        (),
        {
            "body": {
                "scope_type": "zone",
                "scope_value": ZONE_ID,
                "action": "reject",
                "ltv_cap": 0.70,
                **kwargs,
            },
            "ip": "127.0.0.1",
            "user": user,
        },
    )()
    return p6.handle_preview(ctx)


def test_default_no_pagination_backward_compatible(monkeypatch):
    # §1e-1：handle_preview 不带分页参数 → rows==rows_full[:50]、rows_truncated 与旧公式一致。
    snap = _snapshot(60)
    full = _reduce(snap, monkeypatch, page_size=10000)["rows"]
    assert len(full) == 60
    default = _reduce(snap, monkeypatch)
    assert default["rows"] == full[:50]
    assert default["rows_total"] == 60
    assert default["rows_truncated"] == max(0, len(full) - 50) == 10
    assert default["page"] == 1 and default["page_size"] == 50


def test_pagination_60_hits_page2_and_page1(monkeypatch):
    # §1e-2：造 60 笔命中，page=2/page_size=50 ⇒ len==10 rows_total==60 rows_truncated==0
    snap = _snapshot(60)
    p2 = _reduce(snap, monkeypatch, page=2, page_size=50)
    assert len(p2["rows"]) == 10
    assert p2["rows_total"] == 60
    assert p2["rows_truncated"] == 0
    assert p2["page"] == 2 and p2["page_size"] == 50
    # page=1 ⇒ len==50 rows_truncated==10
    p1 = _reduce(snap, monkeypatch, page=1, page_size=50)
    assert len(p1["rows"]) == 50
    assert p1["rows_total"] == 60
    assert p1["rows_truncated"] == 10
    # 两页拼接 = 全量，且互不重叠（分页切片正确性）
    ids_full = {r["loan_id"] for r in _reduce(snap, monkeypatch, page_size=10000)["rows"]}
    ids_p1 = {r["loan_id"] for r in p1["rows"]}
    ids_p2 = {r["loan_id"] for r in p2["rows"]}
    assert ids_p1 | ids_p2 == ids_full and not (ids_p1 & ids_p2)


def test_page_size_large_returns_full(monkeypatch):
    # §1e-3：page_size 一次取全量 ⇒ len(rows)==rows_total==hit.loans
    snap = _snapshot(60)
    out = _reduce(snap, monkeypatch, page_size=10000)
    assert len(out["rows"]) == out["rows_total"] == 60
    assert out["hit"]["loans"] == 60
    assert out["rows"] == _reduce(snap, monkeypatch, page_size=10000)["rows"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page": 0, "page_size": 50},
        {"page": 1, "page_size": 0},
        {"page": 1, "page_size": 10001},
    ],
)
def test_invalid_pagination_raises_valueerror(monkeypatch, kwargs):
    # §1e-4：page<1 / page_size<1 / page_size>10000 ⇒ ValueError（handle_preview 仍拦）
    snap = _snapshot(5)
    with pytest.raises(ValueError):
        _reduce(snap, monkeypatch, **kwargs)


def test_pii_no_customer_fields_any_page(monkeypatch):
    # §1e-5：PII 红线——任一页 rows（含全量页）序列化后均无 customer_id/customer_name
    snap = _snapshot(60)
    for out in [
        _reduce(snap, monkeypatch),  # 默认第 1 页
        _reduce(snap, monkeypatch, page=2, page_size=50),
        _reduce(snap, monkeypatch, page_size=10000),  # 全量页
    ]:
        ser = json.dumps(out, ensure_ascii=False)
        assert "customer_id" not in ser, "预览返回含 customer_id（PII 红线被破坏）"
        assert "customer_name" not in ser, "预览返回含 customer_name（PII 红线被破坏）"
    # 字段白名单：明细允许的列都在列内（不含任何 customer_*）
    row = _reduce(snap, monkeypatch, page_size=10000)["rows"][0]
    for k in row:
        assert not k.startswith("customer_"), f"明细字段 {k!r} 违反 PII 白名单"

    # 路线 B：cover 汇总 + 逐笔也应随响应返回，且口径自洽（cover+hit==in_scope）。
    full = _reduce(snap, monkeypatch, page_size=10000)
    assert "cover" in full and "rows" in full["cover"], "响应须含覆盖存量逐笔"
    # 本快照 60 笔全部命中 → cover 为 0 笔；构造有未命中的场景验证自洽。
    zero_hit = _snapshot(60)
    for i, ln in enumerate(zero_hit["loans"]):
        ln["ltv"] = round(0.20 + 0.001 * i, 4)  # 全部 < cap 0.70 → 全部覆盖、零命中
    out2 = _reduce(zero_hit, monkeypatch, page_size=10000)
    assert out2["hit"]["loans"] == 0 and out2["cover"]["loans"] == 60
    assert out2["cover"]["loans"] + out2["hit"]["loans"] == out2["in_scope"]["loans"]
    assert (
        round(out2["cover"]["balance"] + out2["hit"]["balance"], 2) == out2["in_scope"]["balance"]
    )
    # cover 逐笔保留 addr/industry（PII 红线仅限借款人标识；地址/行业随行供导出结论复核）
    for r in out2["cover"]["rows"]:
        assert "addr" in r and "industry" in r, f"cover 逐笔缺 addr/industry（导出结论需复核）：{r}"


# ---------------- handle_preview 参数透传（§1e-6） ----------------


def _preview_ctx(monkeypatch, body):
    """用内存快照替代 DB 快照，构造 ctx 调 handle_preview。"""
    snap = _snapshot(60)
    monkeypatch.setattr(p6, "_load_snapshot", lambda: snap)
    user = {"user": "admin", "role": "admin", "label": "系统管理员"}
    return type("_Ctx", (), {"body": body, "ip": "127.0.0.1", "user": user})()


def test_handle_preview_parses_and_forwards_pagination(monkeypatch):
    # §1e-6：ctx.body 的 page/page_size 被正确解析并透传给 _impact
    ctx = _preview_ctx(
        monkeypatch,
        {
            "scope_type": "zone",
            "scope_value": ZONE_ID,
            "action": "reject",
            "ltv_cap": 0.70,
            "page": 2,
            "page_size": 50,
        },
    )
    out = p6.handle_preview(ctx)
    assert out["page"] == 2 and out["page_size"] == 50
    assert out["rows_total"] == 60
    assert len(out["rows"]) == 10
    assert out["rows_truncated"] == 0
    # 缺省透传：不带分页参数 → 默认 page=1 page_size=50
    ctx2 = _preview_ctx(
        monkeypatch,
        {
            "scope_type": "zone",
            "scope_value": ZONE_ID,
            "action": "reject",
            "ltv_cap": 0.70,
        },
    )
    out2 = p6.handle_preview(ctx2)
    assert out2["page"] == 1 and out2["page_size"] == 50
    assert len(out2["rows"]) == 50 and out2["rows_truncated"] == 10


@pytest.mark.parametrize(
    "body",
    [
        {"page": 0},
        {"page": 1, "page_size": 0},
        {"page": 1, "page_size": 10001},
        {"page": "abc"},
        {"page_size": "xyz"},
    ],
)
def test_handle_preview_invalid_pagination_raises(monkeypatch, body):
    # §1e-6：非法分页参数 → ValueError（app.py 统一转 400）
    payload = {
        "scope_type": "zone",
        "scope_value": ZONE_ID,
        "action": "reject",
        "ltv_cap": 0.70,
        **body,
    }
    ctx = _preview_ctx(monkeypatch, payload)
    with pytest.raises(ValueError):
        p6.handle_preview(ctx)
