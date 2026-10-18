"""P10 校准证据明细两条只读接口（evidence_rows / evidence_page）的测试集。

零 DB、零 HTTP：直接 import handler 构造 ctx（同 test_p10_sandbox.py 风格）。
两条接口都会重算 persona 管线，报告 params 用 n=50 的小样本把耗时压住。

四条回归防线：
1. 权限闸门：非 admin 两个接口都必须 403，不退化成 200 + allowed:False；
2. query 取值：app.py 的 parse_qs 交来的是 list[str]，不标量化会让 ?max_rows 恒定 400；
3. 同源性：两条接口必须用同一套 n/seed 重算，否则 iframe 页底部
   「本表与 R2-12 例卡同源：同 seed、同 n」就是假话；
4. 脱敏白名单：行内键集合恰好等于约定值，响应文本里不得出现任何 PII 邻域字段名。
"""

import html
import json
import re

import pages.p10_sandbox as mod
import pytest

# 行内白名单：与 _build_desensitized_rows 的产出逐字一致（命名对齐报告 example_cases；
# industry/city 是画像的属性维度——脱敏到门类/市级，供判读规则作用域是否命中）。
_ROW_KEYS = {
    "idx",
    "city",
    "industry",
    "income_monthly_naive",
    "income_monthly_cal",
    "debt_ratio_naive",
    "debt_ratio_cal",
    "credit_score_naive",
    "credit_score_cal",
    "default_prob_naive",
    "default_prob_calibrated",
    "delta_pp",
}

# PII / 业务主键邻域字段名：出现在响应文本里就算泄漏（哪怕值已经脱敏）。
_PII_TOKENS = (
    "customer_id",
    "customer_name",
    "property_addr",
    "id_card",
    "loan_id",
    "phone",
    "address",
    "身份证",
)

_DATA_SCRIPT_RE = re.compile(r'<script id="data" type="application/json">(.*?)</script>', re.S)


@pytest.fixture
def report(tmp_path):
    """params.n=50 / seed=7 的报告：既验证「缺省读 params」，又只让管线算 50 笔。"""
    p = tmp_path / "persona_report.json"
    p.write_text(
        json.dumps(
            {"generated_at": "2026-08-30T17:41:28", "params": {"n": 50, "seed": 7}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return p


def _qs(**kv):
    """复刻 parse_qs 的产物：{"max_rows": ["5"]} —— 值必须是 list。"""
    return {k: [str(v)] for k, v in kv.items()}


def _injected(page):
    """取 iframe 页里后端一次注入的数据块。"""
    return json.loads(_DATA_SCRIPT_RE.search(page).group(1))


# ---------------------------------------------------------------- 路由与权限闸门
def test_routes_registered_on_merged_page():
    """两条新路由挂在合并页（策略推演中心）PAGE 上，否则前端 iframe 直接 404。"""
    from pages import p6_policy

    routes = p6_policy.PAGE["routes"]
    assert routes[("GET", "/api/sandbox/evidence_rows")] is mod.get_sandbox_evidence_rows
    assert routes[("GET", "/api/sandbox/evidence_page")] is mod.get_sandbox_evidence_page


def test_non_admin_rows_returns_403(monkeypatch, ctx, report):
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(report))
    ctx.user = {"user": "risk", "role": "risk", "label": "风险策略"}
    result = mod.get_sandbox_evidence_rows(ctx)
    assert isinstance(result, tuple) and result[0] == 403
    assert "仅对管理员开放" in result[1]["error"]


def test_non_admin_page_returns_403_html(monkeypatch, ctx, report):
    """403 语义保持不变（iframe 会正常渲染这页），只在文案里给出升级路径。"""
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(report))
    ctx.user = {"user": "da", "role": "da", "label": "数据分析"}
    code, payload = mod.get_sandbox_evidence_page(ctx)
    assert code == 403 and "__html__" in payload
    assert "管理员" in payload["__html__"]


# ---------------------------------------------------------------- query 标量化（P0-1）
def test_max_rows_query_is_scalarised(monkeypatch, ctx, report):
    """?max_rows=5 → 200 且恰好 5 行：parse_qs 的 ["5"] 直穿 int() 会抛 TypeError。"""
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(report))
    ctx.query = _qs(max_rows=5)
    out = mod.get_sandbox_evidence_rows(ctx)
    assert isinstance(out, dict) and out["ok"] is True
    assert out["row_count"] == 5
    assert [r["idx"] for r in out["rows"]] == [0, 1, 2, 3, 4]
    # n / seed 缺省来自报告 params（不是接口里的硬编码 1000 / 42）。
    assert out["n"] == 50 and out["seed"] == 7
    assert out["generated_at"] == "2026-08-30T17:41:28"


def test_no_query_uses_report_params(monkeypatch, ctx, report):
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(report))
    out = mod.get_sandbox_evidence_rows(ctx)
    assert out["row_count"] == 50 and out["n"] == 50


@pytest.mark.parametrize(
    "query",
    [
        _qs(n="abc"),
        _qs(seed="x"),
        _qs(max_rows="abc"),
        _qs(max_rows=0),
        _qs(max_rows=-3),
        _qs(seed=-1),
        _qs(n=0),
        _qs(n=5001),
    ],
)
def test_bad_params_return_400(monkeypatch, ctx, report, query):
    """坏参与越界一律 400，不把用户可构造的非法输入变成 5xx。

    n 上限防 admin 触发超大重算；max_rows 下界防 0 与负数被下游守卫当成「未设置」
    而静默返回全量行；seed 负值会让 numpy 抛 "expected non-negative integer"，
    不在参数层拦掉就是 500，还会把内部异常串吐进 iframe 错误页。
    """
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(report))
    ctx.query = dict(query)
    code, payload = mod.get_sandbox_evidence_rows(ctx)
    assert code == 400 and payload["error"]

    ctx.query = dict(query)
    code2, payload2 = mod.get_sandbox_evidence_page(ctx)
    assert code2 == 400 and "__html__" in payload2


# ---------------------------------------------------------------- 脱敏白名单
def test_rows_whitelist_and_no_pii(monkeypatch, ctx, report):
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(report))
    ctx.query = _qs(max_rows=10)
    out = mod.get_sandbox_evidence_rows(ctx)
    assert len(out["rows"]) == 10
    for row in out["rows"]:
        assert set(row) == _ROW_KEYS
        # 行业/城市随基准行抽样带出、校准不改写：明细表靠它们判读规则作用域。
        assert row["industry"] and row["city"]
    blob = json.dumps(out, ensure_ascii=False)
    for token in _PII_TOKENS:
        assert token not in blob
    # features 只列三个数值特征名：每个特征在行内以 <feature>_naive / <feature>_cal 两列落地
    # （industry/city 是属性维度，没有前后对照，不入 features 清单）。
    assert out["features"] == ["income_monthly", "debt_ratio", "credit_score"]
    for feat in out["features"]:
        assert f"{feat}_naive" in _ROW_KEYS and f"{feat}_cal" in _ROW_KEYS


# ---------------------------------------------------------------- 同源性（P0-3）
def test_page_and_rows_share_default_report_params(monkeypatch, ctx, report):
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(report))
    payload = mod.get_sandbox_evidence_rows(ctx)
    ctx.query = _qs(lang="zh-CN")
    code, html_payload = mod.get_sandbox_evidence_page(ctx)
    assert code == 200
    assert _injected(html_payload["__html__"])["rows"] == payload["rows"]


def test_page_and_rows_share_query_override(monkeypatch, ctx, report):
    """缺省同源不等于覆盖同源：显式 ?n=&seed= 时两条接口仍必须给出同一批数据。"""
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(report))
    ctx.query = _qs(n=20, seed=3)
    payload = mod.get_sandbox_evidence_rows(ctx)
    assert payload["n"] == 20 and payload["row_count"] == 20

    ctx.query = _qs(n=20, seed=3, lang="en")
    code, html_payload = mod.get_sandbox_evidence_page(ctx)
    assert code == 200
    assert _injected(html_payload["__html__"])["rows"][0] == payload["rows"][0]


# ---------------------------------------------------------------- 三语 + 模板占位
@pytest.mark.parametrize(
    "lang,col_idx_label",
    [("zh-CN", "序号"), ("en", "Idx"), ("zh-HK", "序號")],
)
def test_evidence_page_is_trilingual(monkeypatch, ctx, report, lang, col_idx_label):
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(report))
    monkeypatch.setattr(mod, "_build_desensitized_rows", lambda **kw: ([], None))
    ctx.query = _qs(lang=lang)
    code, payload = mod.get_sandbox_evidence_page(ctx)
    assert code == 200
    page = payload["__html__"]
    t = _injected(page)["t"]
    assert _injected(page)["lang"] == lang
    assert t["col_idx"] == col_idx_label
    assert t["filter"] and t["sort"] and t["note"]
    # P1-4：<html lang> 与 <title> 都随语言走，不留字面占位。
    assert f'<html lang="{lang}">' in page
    assert html.escape(t["title"], quote=False) in page
    for dead in ("__TITLE__", "__LANG__", "__DATA_JSON__"):
        assert dead not in page


def test_unknown_lang_falls_back_to_zh_cn(monkeypatch, ctx, report):
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(report))
    monkeypatch.setattr(mod, "_build_desensitized_rows", lambda **kw: ([], None))
    ctx.query = _qs(lang="fr")
    code, payload = mod.get_sandbox_evidence_page(ctx)
    assert code == 200
    page = payload["__html__"]
    assert _injected(page)["lang"] == "zh-CN" and '<html lang="zh-CN">' in page


# ---------------------------------------------------------------- iframe 高亮可见性
def test_row_highlight_targets_td():
    """P0-2：偶数行 td 的斑马纹背景不透明，高亮只写在 tr 上等于没有反馈。"""
    tpl = mod._EVIDENCE_PAGE_TEMPLATE
    assert "row-hl > td" in tpl, "高亮规则未命中 td"
    assert "classList.add('row-hl')" in tpl and "classList.remove('row-hl')" in tpl
    assert "style.background" not in tpl, "仍在用 tr 内联背景（会被 td 盖住）"
