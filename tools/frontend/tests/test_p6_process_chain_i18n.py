"""批次②：传导链 #p10-trace 双轨不变量（仿 test_p6_stress.py 的 _assert_note_double_track）。

守门：每个出口的「中文原句 == *_key.format(**_*_params)」，且 step 必有 step_key。
params 里 district/label 是中文数据值（前端 i18nParams 直出原文，不翻译），数字原样回填，
故 Python 侧 key.format 即等价于 zh-CN 前端渲染结果——漂移立刻红。
"""

import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
spec = importlib.util.spec_from_file_location(
    "tp_scenario", os.path.join(os.path.dirname(__file__), "test_p6_policy_scenario.py")
)
tp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tp)
spec2 = importlib.util.spec_from_file_location(
    "tg_geo", os.path.join(os.path.dirname(__file__), "test_p6_scenario_geo.py")
)
tg = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(tg)

import pages.p6_policy as p6  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_boundary_data(monkeypatch):
    # 区名限定情景的坐标闸门映射默认按「边界数据不可用」stub：本文件的单测不走
    # _load_snapshot，也绝不能依赖本地缓存文件 / 外网——映射 None 让
    # _loan_in_shock_geo 走地址子串兜底（旧行为），坐标优先路径由显式注入映射的
    # 用例（test_p6_income_dti）单独覆盖。
    monkeypatch.setattr(p6, "_district_loan_map", lambda snap, city: (None, None))


_JS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "static", "pages", "p6_policy.js"
)
with open(_JS, encoding="utf-8") as _f:
    JS_TEXT = _f.read()

# 复刻前端 CITY_CODES（zh-CN 字典的 city→中文名），仅供 zh-CN 等价断言用
_CITY = {"gz": "广州", "sz": "深圳"}


def _assert_step_double_track(step, tag, idx):
    # step 标签必须有双轨 key
    assert step.get("step_key") == step["step"], (
        f"{tag}[{idx}] step_key 漂移: {step.get('step_key')!r} vs {step['step']!r}"
    )
    # detail 双轨
    if step.get("detail_key") is None:
        # scenario_math 内部 lines/note（本轮不双轨，归 scenario_math 一批）回落中文原句
        assert step["step"] == "传导测算（示意）", (
            f"{tag}[{idx}] 非预期的无 detail_key 步: {step['step']!r}"
        )
    else:
        params = step.get("detail_params") or {}
        assert step["detail_key"].format(**params) == step["detail"], (
            f"{tag}[{idx}] DETAIL 双轨漂移:\n key={step['detail_key']!r}\n params={params!r}\n "
            f"回填={step['detail_key'].format(**params)!r}\n 原句={step['detail']!r}"
        )
    # basis 双轨：basis_key 为 None 的合法态只有两种——mismatch / 单步说明步根本没有
    # basis 字段，或 math_block 示意步 mb 未给 basis_key。有中文原句却无 key 属双轨
    # 失守（en/zh-HK 会裸奔这句中文），必须红。
    if step.get("basis_key") is None:
        assert not step.get("basis"), (
            f"{tag}[{idx}] basis 有原句但 basis_key 缺失（en/zh-HK 将裸奔中文）: {step['basis']!r}"
        )
    else:
        bparams = step.get("basis_params") or {}
        assert step["basis_key"].format(**bparams) == step["basis"], (
            f"{tag}[{idx}] BASIS 双轨漂移:\n key={step['basis_key']!r}\n params={bparams!r}\n "
            f"回填={step['basis_key'].format(**bparams)!r}\n 原句={step['basis']!r}"
        )


def _assert_chain(steps, tag):
    assert steps, f"{tag}: 空链"
    for i, s in enumerate(steps):
        _assert_step_double_track(s, tag, i)


def test_process_chain_exit_price_drop_non_district():
    # 出口 C：无区级限定的房价冲击（完整 3 步，含 flip_band 版 basis）
    shock = {"kind": "price_drop", "pct": 0.10, "label": "房价下跌 10%"}
    imp = p6._impact(tp._snapshot(), "zone", tp.ZONE_ID, "reject", 0.70, shock=shock)
    _assert_chain(p6._process_chain(imp, shock, None), "C_non_district")


def test_process_chain_exit_district_with_outside():
    # 出口 C：区级冲击 × 异区笔（作用域内含域外笔，outside>0 分支 detail）
    imp = p6._impact(
        tg._geo_snapshot(), "zone", tg.ZONE_ID, "reject", 0.70, shock=tg._DISTRICT_SHOCK
    )
    _assert_chain(p6._process_chain(imp, tg._DISTRICT_SHOCK, None), "C_district_outside")


def test_process_chain_exit_district_zero_hits():
    # 出口 B：区级情景 × 作用域内无该区存量（单步说明，0 命中）
    only_tianhe = {
        "loans": [tg._loan(2, 0.80, "广州市天河区体育西路2号")],
        "zones": tg._geo_snapshot()["zones"],
    }
    shock = dict(tg._DISTRICT_SHOCK)
    imp = p6._impact(only_tianhe, "zone", tg.ZONE_ID, "reject", 0.70, shock=shock)
    _assert_chain(p6._process_chain(imp, shock, None), "B_district_zero_hits")


def test_process_chain_exit_district_missing_ltv():
    # 出口 C：地域内笔全部缺估值 → 跳过「传导测算（逐笔）」步，规则评估走整区命中版 basis
    snap = {
        "loans": [
            tg._loan(1, None, "广州市越秀区中山一路1号"),
            tg._loan(2, 0.80, "广州市天河区体育西路2号"),
        ],
        "zones": tg._geo_snapshot()["zones"],
    }
    shock = dict(tg._DISTRICT_SHOCK)
    imp = p6._impact(snap, "zone", tg.ZONE_ID, "reject", 0.70, shock=shock)
    _assert_chain(p6._process_chain(imp, shock, None), "C_district_missing_ltv")


def test_process_chain_exit_math_block():
    # 出口 D：收入类情景走 math_block（情景解析步双轨；传导测算（示意）步回落中文）
    mb = p6.scenario_math({"kind": "income_drop", "pct": 0.15, "label": "收入下降 15%"})
    imp = p6._impact(tp._snapshot(), "zone", tp.ZONE_ID, "reject", 0.70)
    _assert_chain(p6._process_chain(imp, None, mb), "D_math")


def test_process_chain_exit_mismatch():
    # 出口 A：城市核对不符（冷分支，detail_key 退化为整句 key + 空 params，仍守不变量）
    imp = p6._impact(tp._snapshot(), "zone", tp.ZONE_ID, "reject", 0.70)
    steps = p6._process_chain(imp, None, None, mismatch="情景限定广州…无影响")
    _assert_chain(steps, "A_mismatch")


def test_a6_mb_detail_key_registered_double_track():
    # FIX 1：后端 mb_detail_key（lines_key 用「；」join + 「；」+ note_key）必须能在
    # en/zh-HK 字典找到整体注册的 key（防「。」回退导致 en/zh-HK 漏翻）。
    for sc in (
        {"kind": "rate_change", "pp": -1.0, "label": "贷款利率下调 1 个百分点"},
        {"kind": "rate_change", "pp": 0.5, "label": "贷款利率上调 0.5 个百分点"},
        {"kind": "income_drop", "pct": 0.15, "label": "收入下降 15%"},
    ):
        m = p6.scenario_math(sc)
        key = "；".join(m["lines_key"]) + "；" + (m.get("note_key") or m["note"])
        # key 必须由 lines_key + note_key 拼接得到（不是「。」）
        assert "。" not in key, f"拼接符回退成句号：{key}"
        assert JS_TEXT.count(f'"{key}"') >= 2, f"A6 拼接 key 未双语注册：{key}"


def test_process_chain_mismatch_double_track_city_scope():
    # FIX 3：城市核对不符步双轨。深圳房价下跌 10% 打到广州城市作用域 → mismatch。
    sc = p6.parse_scenario("深圳房价下跌 10%")
    assert sc and sc["kind"] == "price_drop" and sc["city"] == "sz"
    zones = {}
    shock, mismatch = p6._resolve_shock(sc, "city", "gz", zones)
    assert shock is None and mismatch, "应触发城市核对不符"
    steps = p6._process_chain({}, None, None, mismatch, sc, "city", "gz", zones)
    assert len(steps) == 1
    s = steps[0]
    assert s["step_key"] == "情景核对"
    exp_key = (
        "情景限定{geo}，与作用域（{stype}{sval}）不在同一城市——该情景对本次推演的定量结果无影响"
    )
    assert s["detail_key"] == exp_key, s["detail_key"]
    # geo=城市码 sz，stype=字面量「城市 · 」，sval=城市码 gz
    assert s["detail_params"] == {"geo": "sz", "stype": "城市 · ", "sval": "gz"}, s["detail_params"]
    # 双语注册
    assert JS_TEXT.count(f'"{exp_key}"') >= 2, "mismatch 模板未双语注册"
    for st in ("城市 · ", "网格 ", "行业 · "):
        assert JS_TEXT.count(f'"{st}"') >= 2, f"mismatch 作用域类型字面量未双语注册：{st}"
    # zh-CN 逐字：模拟 i18nParams 把 geo/sval 经 CITY_CODES 翻名、stype 原样，渲染 == 原中文 detail
    sim = exp_key.format(
        geo=_CITY[s["detail_params"]["geo"]],
        stype=s["detail_params"]["stype"],
        sval=_CITY[s["detail_params"]["sval"]],
    )
    assert sim == s["detail"], f"zh-CN 逐字对不上:\n 渲染={sim!r}\n 原句={s['detail']!r}"


def test_geofence_key_no_english_record():
    # FIX 2：geofence key 不再混英文 record/records（zh-CN KEY 含「记录」，zh-HK VALUE 含「記錄」）
    # 坐标优先改造后「仅地址含」改为与数据源无关的中性表述「仅位于」。
    key_zh = "地域闸门：仅位于{district}的 记录 参与冲击；作用域内命中 0 笔 → LTV 分布与命中判定均按原口径"
    assert JS_TEXT.count(f'"{key_zh}"') >= 2, "geofence key（简体）未双语注册"
    assert "仅地址含" not in JS_TEXT, "前端仍有旧的地址口径 geofence 词条"
    assert "的 record 参与" not in JS_TEXT, "前端中文语境仍有英文 record 泄漏"
    assert "的 記錄 參與" in JS_TEXT, "zh-HK value 应为繁体「記錄」"
    # 后端 basis/basis_key 字段也应已是「记录」（docstring 里的英文 record 是注释，不泄漏，排除）
    import inspect

    src = inspect.getsource(p6)
    assert key_zh in src, (
        "后端 basis/basis_key 未逐字使用前端注册的 geofence 词条（应为「记录」而非英文 record）"
    )
