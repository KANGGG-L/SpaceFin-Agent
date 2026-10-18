"""批次② · scenario_math 与 shock.label 双轨不变量（仿 test_p6_process_chain_i18n）。

守门：每个双轨字段「中文原句 == *_key.format(**_*_params)」，且 key 在 en/zh-HK 两字典齐备。
params 里只有数字（pct/mag/p0/p1/delta/base/r1/factor 等）与方向词，i18nParams 过 t() 后原样回填，
故 Python 侧 key.format 即等价于目标语言前端渲染结果——漂移立刻红。
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

import pages.p6_policy as p6

_BASE_RATE_PCT = p6._BASE_RATE_PCT


def test_scenario_math_rate_change_double_track():
    # 利率下调：lines / note / basis 全部双轨
    sc = {"kind": "rate_change", "pp": -1.0, "label": "贷款利率下调 1 个百分点"}
    m = p6.scenario_math(sc)
    assert m["lines_key"] is not None and len(m["lines_key"]) == len(m["lines"])
    for k, params, orig in zip(m["lines_key"], m["lines_params"], m["lines"], strict=False):
        assert k.format(**params) == orig, (
            f"lines 漂移:\n key={k!r}\n params={params!r}\n 回填={k.format(**params)!r}\n 原句={orig!r}"
        )
    assert m["note_key"].format(**m["note_params"]) == m["note"], "note 漂移"
    assert m["basis_key"].format(**m["basis_params"]) == m["basis"], "basis 漂移"


def test_scenario_math_rate_change_up_double_track():
    # 利率上调分支：note 走「上调」模板
    sc = {"kind": "rate_change", "pp": 0.5, "label": "贷款利率上调 0.5 个百分点"}
    m = p6.scenario_math(sc)
    assert m["note_key"].format(**m["note_params"]) == m["note"]
    assert "上调" in m["note_key"]


def test_scenario_math_income_drop_double_track():
    # 收入下降：lines / note / basis 全部双轨（note 无 label）
    sc = {"kind": "income_drop", "pct": 0.15, "label": "收入下降 15%"}
    m = p6.scenario_math(sc)
    for k, params, orig in zip(m["lines_key"], m["lines_params"], m["lines"], strict=False):
        assert k.format(**params) == orig, (
            f"lines 漂移:\n key={k!r}\n 回填={k.format(**params)!r}\n 原句={orig!r}"
        )
    assert m["note_key"].format(**m["note_params"]) == m["note"]
    assert m["basis_key"].format(**m["basis_params"]) == m["basis"]


def test_parse_scenario_label_double_track():
    # 三类情景 label 双轨：中文原 label == label_key.format(label_params)
    cases = [
        ("房价下跌 10%", "房价下跌 {pct}%", {"pct": "10"}),
        ("收入下降 15%", "收入下降 {pct}%", {"pct": "15"}),
        ("贷款利率下调 1 个百分点", "贷款利率下调 {mag} 个百分点", {"mag": "1"}),
        ("贷款利率上调 0.5 个百分点", "贷款利率上调 {mag} 个百分点", {"mag": "0.5"}),
    ]
    for text, exp_key, exp_params in cases:
        s = p6.parse_scenario(text)
        assert s is not None, text
        assert s["label_key"] == exp_key, f"{text}: label_key 漂移 {s['label_key']!r}"
        assert s["label_params"] == exp_params, f"{text}: label_params 漂移 {s['label_params']!r}"
        assert s["label_key"].format(**s["label_params"]) == s["label"], (
            f"{text}: 中文原 label 与 key.format 不符\n key={s['label_key']!r} params={s['label_params']!r}"
        )


def test_dict_parity_for_scenario_math_keys():
    # 全部新双轨 key 须 en/zh-HK 双语齐备（与 test_fe_dict_parity_for_new_keys 同源纪律）
    _JS = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "static", "pages", "p6_policy.js"
    )
    with open(_JS, encoding="utf-8") as f:
        JS_TEXT = f.read()
    keys = set()
    m = p6.scenario_math({"kind": "rate_change", "pp": -1.0, "label": "贷款利率下调 1 个百分点"})
    keys.update(m["lines_key"])
    keys.add(m["note_key"])
    keys.add(m["basis_key"])
    m = p6.scenario_math({"kind": "income_drop", "pct": 0.15, "label": "收入下降 15%"})
    keys.update(m["lines_key"])
    keys.add(m["note_key"])
    keys.add(m["basis_key"])
    for s in (
        p6.parse_scenario("房价下跌 10%"),
        p6.parse_scenario("收入下降 15%"),
        p6.parse_scenario("贷款利率下调 1 个百分点"),
        p6.parse_scenario("贷款利率上调 1 个百分点"),
    ):
        keys.add(s["label_key"])
    for key in keys:
        assert JS_TEXT.count(f'"{key}"') >= 2, f"词条未双语齐备：{key}"
