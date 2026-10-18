"""批次③-A · R2-10（0.70 / 0.85 取值依据随 env 下发）。

后端：`thresholds` 载荷下发 `suggest_ltv_cap_basis` 与 `ltv_red_line_basis` 两条**文案**，
取值随 env 生效（工单验收：RISK_LTV_RED_LINE=0.9 后依据文字也要跟着变，证明不是硬编码）；
前端：`#p6-ltv-cap` 的 `placeholder` 由 `st.data.thresholds.suggest_ltv_cap` 填，
且全站只此一处来源——阈值不得写死在源码里。

本项**不含**把 app.js 的 LTV_WARN_LINE / LTV_RED_LINE 收敛为单一来源（另开工单），
故本文件只判 p6_policy.js，不判 app.js / p5_spatial.js / sandbox_agent.py。
"""

import re
from pathlib import Path

import db
import pages.p6_policy as p6
from fakeconn import FakeConn

ROOT = Path(__file__).resolve().parents[3]
JS = ROOT / "tools" / "frontend" / "static" / "pages" / "p6_policy.js"

ZONE_ID = "price-gz-113.30-23.14"
_OLD_THRESHOLD_KEYS = {
    "ltv_red_line",
    "class_ltv_upper",
    "zone_match_radius_km",
    "suggest_ltv_cap",
}


def _js_text() -> str:
    return JS.read_text(encoding="utf-8")


def test_threshold_bounds_come_from_backend():
    """LTV / DTI 的合法区间只能有一个来源：后端 thresholds。

    此前前端骨架硬编码 min="1" max="200"（LTV）与 min="30" max="95"（DTI），
    后端又是另一份 0.01–2 与 0.30–0.95——改口径要动四处，漏一处就是
    「前端放行了、后端驳回」。现在前端一律读下发值。
    """
    text = _js_text()
    assert 'min="1"' not in text and 'max="200"' not in text, "LTV 区间又硬编回骨架了"
    assert 'min="30"' not in text and 'max="95"' not in text, "DTI 区间又硬编回骨架了"
    assert "function applyThresholdBounds()" in text, "缺区间下发的应用函数"
    assert "th.ltv_cap_min" in text and "th.dti_red_min" in text, "区间未读后端下发值"


def _thresholds(monkeypatch):
    """跑一次 handle_list（零 DB：快照换内存数据、规则表换空假库）拿 thresholds。"""
    monkeypatch.setattr(p6, "ensure_policy_table", lambda: None)
    monkeypatch.setattr(
        p6,
        "_load_snapshot",
        lambda: {
            "loans": [],
            "zone_loans": {},
            "zones": {
                ZONE_ID: {
                    "zone_id": ZONE_ID,
                    "zone_type": "price",
                    "city": "gz",
                    "center_lat": 23.14,
                    "center_lng": 113.30,
                    "sample_count": 1,
                    "median_unit_price": 30000.0,
                    "median_ltv": 0.7,
                    "price_dev_vs_city": -0.1,
                    "is_high_risk_zone": 0,
                    "high_risk_rule": "price_low",
                }
            },
        },
    )
    monkeypatch.setattr(db, "crawl_conn", lambda: FakeConn())
    ctx = type("_Ctx", (), {"body": {}, "ip": "-", "user": {"user": "u", "role": "admin"}})()
    return p6.handle_list(ctx)["thresholds"]


# ---------------- 后端载荷 ----------------


def test_thresholds_gains_exactly_two_basis_fields(monkeypatch):
    """载荷新增字段：中文原句 *basis（后端消费方兜底）+ 结构化 *_basis_key/_params（前端 i18n）。

    另含 4 个合法区间字段（ltv_cap_min/max、dti_red_min/max）：它们把原先前后端各写
    一份的魔法数字收成后端单一来源，前端改读下发值。口径变化必须反映到这里，
    否则「前端放行、后端驳回」的裂缝会悄悄回来。
    """
    th = _thresholds(monkeypatch)
    assert set(th) == _OLD_THRESHOLD_KEYS | {
        "suggest_ltv_cap_basis",
        "ltv_red_line_basis",
        "ltv_cap_min",
        "ltv_cap_max",
        "dti_red_min",
        "dti_red_max",
        "ltv_red_line_basis_key",
        "ltv_red_line_basis_params",
        "suggest_ltv_cap_basis_key",
        "suggest_ltv_cap_basis_params",
        # DTI 红线：推演中心可设置（POST /api/policy/setting），非引擎常量
        "dti_red_line",
    }, f"thresholds 键集异常：{sorted(th)}"
    assert isinstance(th["ltv_red_line_basis"], str) and th["ltv_red_line_basis"]
    assert isinstance(th["suggest_ltv_cap_basis"], str) and th["suggest_ltv_cap_basis"]
    # key 与 params 必须成对下发：前端 t(key, params) 按同名占位符渲染。
    assert (
        th["ltv_red_line_basis_key"] in th["ltv_red_line_basis"]
        or "{red}" in th["ltv_red_line_basis_key"]
    )
    assert (
        isinstance(th["ltv_red_line_basis_params"], dict)
        and "red" in th["ltv_red_line_basis_params"]
    )
    assert isinstance(th["suggest_ltv_cap_basis_params"], dict)
    assert {"red", "cap"} <= set(th["suggest_ltv_cap_basis_params"])
    # zh-CN 原句 = key.format(params)：保证前端 key 模板与后端留痕文案逐字同源。
    assert th["ltv_red_line_basis"] == th["ltv_red_line_basis_key"].format(
        **th["ltv_red_line_basis_params"]
    )
    assert th["suggest_ltv_cap_basis"] == th["suggest_ltv_cap_basis_key"].format(
        **th["suggest_ltv_cap_basis_params"]
    )


def test_basis_text_is_built_from_env_values_not_hardcoded(monkeypatch):
    default = _thresholds(monkeypatch)
    assert "0.85" in default["ltv_red_line_basis"], "红线依据没带上生效中的红线值"
    assert "0.70" in default["suggest_ltv_cap_basis"]

    monkeypatch.setattr(p6.config, "LTV_RED_LINE", 0.9)
    monkeypatch.setitem(p6.config.CLASS_LTV_UPPER, "关注", 0.88)
    over = _thresholds(monkeypatch)
    assert over["ltv_red_line_basis"] != default["ltv_red_line_basis"], (
        "改 env 后红线依据一字未变 → 依据是硬编码文案"
    )
    assert over["suggest_ltv_cap_basis"] != default["suggest_ltv_cap_basis"], (
        "改 env 后建议上限依据一字未变 → 依据是硬编码文案"
    )
    assert "0.90" in over["ltv_red_line_basis"] and "0.90" in over["suggest_ltv_cap_basis"]
    assert "0.88" in over["suggest_ltv_cap_basis"], "建议依据没引用「关注」级上界的生效值"
    assert "0.85" not in over["ltv_red_line_basis"], "旧默认值仍留在依据里"


def test_basis_text_leaks_no_env_var_names(monkeypatch):
    """§C 保护清单：环境变量名不得出现在可见文案里（第一轮已从 sandbox 清除）。"""
    th = _thresholds(monkeypatch)
    for key in ("ltv_red_line_basis", "suggest_ltv_cap_basis"):
        assert "RISK_LTV" not in th[key] and "os.getenv" not in th[key], th[key]


def test_read_only_payload_top_level_keys_unchanged(monkeypatch):
    """§1 批次③ 记录的同一条口径：/api/policy 只允许 thresholds 内部增量，顶层键不得动。

    `industries` 是 US-01（行业作用域）有意新增的作用域候选键，与
    zones/cities 同级——属于登记在册的增量，不是意外漂移。
    """
    monkeypatch.setattr(p6, "ensure_policy_table", lambda: None)
    monkeypatch.setattr(p6, "_load_snapshot", lambda: {"loans": [], "zone_loans": {}, "zones": {}})
    monkeypatch.setattr(db, "crawl_conn", lambda: FakeConn())
    ctx = type("_Ctx", (), {"body": {}, "ip": "-", "user": {"user": "u", "role": "admin"}})()
    out = p6.handle_list(ctx)
    assert set(out) == {
        "policies",
        "union_impact",
        "zones",
        "cities",
        "tile_url",
        "industries",
        "actions",
        "can_write",
        "thresholds",
    }


# ---------------- 前端显示 ----------------


def test_ltv_cap_placeholder_single_source_is_thresholds():
    text = _js_text()
    assert 'placeholder="0.70"' not in text, (
        "#p6-ltv-cap 的 placeholder 仍把 0.70 硬编在骨架里（§B R2-10 点名的那处）"
    )
    assert text.count('el("p6-ltv-cap").placeholder') == 1, "placeholder 出现了第二处来源"
    m = re.search(r'el\("p6-ltv-cap"\)\.placeholder\s*=\s*([^\n]*)', text)
    assert m, "placeholder 没有由 JS 赋值"
    assert "thresholds.suggest_ltv_cap" in m.group(1), (
        f"placeholder 的赋值来源不是接口下发的 suggest_ltv_cap：{m.group(1)}"
    )


def test_no_hardcoded_ltv_threshold_literal():
    """护栏（替代已下线的 LTV 依据常驻行）：前端不得把 0.70 / 0.85 这类阈值写死在源码里，

    LTV 红线 / 建议上限一律来自接口下发的 thresholds（env 可调），改 env 后页面数字必须跟着变。
    """
    text = _js_text()
    offenders = [
        ln
        for ln in text.splitlines()
        if re.search(r"\b0\.7\d\b", ln) or re.search(r"\b0\.8\d\b", ln)
    ]
    assert not offenders, f"LTV 阈值疑似硬编码在源码里：{offenders}"
