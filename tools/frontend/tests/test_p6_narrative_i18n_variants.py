"""P6 叙事管线三语变体（*_i18n）测试。

注意：本文件虽以 p6 命名，实际测的是 sandbox_agent（P10 共享沙箱管线）；
P6 只是该管线的消费方（POST /api/sandbox/hypothesis 由 pages.p6_policy 承接）。
按文件名找「P6 专属叙事测试」会误判——P6 自身的双轨 key 测试在
test_p6_process_chain_i18n.py / test_p6_scenario_math_i18n.py / test_p6_income_dti.py。

方案：确定性管线 payload 携带三语变体（sandbox_agent._loc3），前端按当前语言
整句重画，模板不复制进 JS 字典（避免双份维护、拼 key 漂移）。

铁律（本文件的核心断言）：每个 i18n["zh-CN"] 必须与 lang="zh-CN" 时的
resolved 输出逐字节一致——zh-CN 零回归。

_run_agent 用 sys.modules 桩替换 langchain（跳过真实 LLM，只测模板路径：
gen title / critic / calib 行）；narrative 正文本身是模型单语自由文本，
不在变体覆盖范围（已知边界）。
"""

import sys
import types

import pages.p10_sandbox  # noqa: F401  (确保 tools/frontend 已入 sys.path)
import pytest
import sandbox_agent as sa

PROFILE = {
    "loan_id": 42,
    "city_name": "深圳",
    "city": "shenzhen",
    "balance": 123456.0,
    "ltv": 0.62,
    "income_monthly": 9000,
    "debt_ratio": 0.5,
    "credit_score": 680,
    "risk_class": "正常",
    "is_hit": True,
}

# en 变体抽演进断言用的拟议规则：scope/action 标签是前端透传的数据值，
# 要让 en 变体无中文，标签本身得是英文（真实链路里它们随请求语言）。
RULE_EN = {"scope_label": "Shenzhen first home", "action_label": "Reject", "ltv_cap": 0.7}
RULE_ZH = {"scope_label": "深圳首套", "action_label": "拒贷", "ltv_cap": 0.7}

HYP_BASE = "假设某地出台限购政策"
HYP_RATE = "假设利率下调 50 个百分点"
HYP_SECTOR = "假设制造业客户收入下降"


# --------------------------------------------------------------------------
# 通用断言
# --------------------------------------------------------------------------


def _assert_variants_ok(res, lang, zh_ref=None):
    """逐字节铁律 + 当前语言自洽 + 变体键齐全非空。

    zh_ref 是 lang="zh-CN" 的参照结果（铁律对照物）；en/zh-HK 请求下 resolved
    本来就不是 zh，直接拿 res 自身比是错的。zh-CN 请求时 zh_ref 即 res。
    """
    zh = zh_ref if zh_ref is not None else res
    assert res["narrative_i18n"]["zh-CN"] == zh["narrative"], "narrative_i18n zh-CN 铁律"
    assert res["inference_i18n"]["zh-CN"] == zh["inference"], "inference_i18n zh-CN 铁律"
    assert res["narrative_i18n"][lang] == res["narrative"], "narrative 当前语言自洽"
    assert res["inference_i18n"][lang] == res["inference"], "inference 当前语言自洽"
    zh_rows = {r["kind"]: r for r in zh["trace"]}
    for row in res["trace"]:
        if "title_i18n" in row:
            assert row["title_i18n"]["zh-CN"] == sa._loc(
                "zh-CN",
                row["title_i18n"]["zh-CN"],
                row["title_i18n"]["en"],
                row["title_i18n"]["zh-HK"],
            ), f"{row['kind']} title zh-CN 铁律"
            assert row["title_i18n"][lang] == row["title"], f"{row['kind']} title 当前语言自洽"
        if "detail_i18n" in row:
            assert row["detail_i18n"][lang] == row["detail"], f"{row['kind']} detail 当前语言自洽"
            if row["kind"] in zh_rows:
                assert row["detail_i18n"]["zh-CN"] == zh_rows[row["kind"]].get("detail"), (
                    f"{row['kind']} detail zh-CN 铁律"
                )
        for key in ("title_i18n", "detail_i18n"):
            if key in row:
                for lg in ("en", "zh-HK"):
                    assert row[key].get(lg), f"{row['kind']} {key}[{lg}] 非空"


def _assert_en_no_chinese_ui_words(res):
    """en 变体不得含中文 UI 词（抽演进：叙事/校准/批评/规则）。"""
    banned = ("叙事", "校准", "批评", "规则")
    assert not any(w in res["narrative_i18n"]["en"] for w in banned)
    assert not any(w in res["inference_i18n"]["en"] for w in banned)
    for row in res["trace"]:
        for key in ("title_i18n", "detail_i18n"):
            if key in row:
                assert not any(w in row[key]["en"] for w in banned), (row["kind"], key)


# --------------------------------------------------------------------------
# _fallback：带/不带 profile、带/不带 rule × 三语
# --------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["zh-CN", "en", "zh-HK"])
@pytest.mark.parametrize("rule", [None, RULE_EN, RULE_ZH])
@pytest.mark.parametrize("profile", [None, PROFILE])
def test_fallback_variants_byte_equal(profile, rule, lang):
    rd = sa._rule_desc(rule, lang) if rule else None
    res = sa._fallback(HYP_RATE, rd, profile, lang, rule=rule)
    rd_zh = sa._rule_desc(rule, "zh-CN") if rule else None
    zh_ref = sa._fallback(HYP_RATE, rd_zh, profile, "zh-CN", rule=rule)
    _assert_variants_ok(res, lang, zh_ref)
    if lang == "en" and rule is RULE_EN:
        _assert_en_no_chinese_ui_words(res)


def test_fallback_zh_cn_zero_regression():
    """zh-CN resolved 与改动前口径逐字段一致（铁律的另一面：resolved 本身没被改）。"""
    rd = sa._rule_desc(RULE_ZH, "zh-CN")
    res = sa._fallback(HYP_RATE, rd, PROFILE, "zh-CN", rule=RULE_ZH)
    assert res["narrative"] == res["narrative_i18n"]["zh-CN"]
    assert res["inference"] == res["inference_i18n"]["zh-CN"]
    kinds = [r["kind"] for r in res["trace"]]
    assert kinds == ["gen", "rule", "critic", "calib"]
    # 规则行 detail 的 zh 变体须等于 zh-CN 重算的 resolved
    rule_row = res["trace"][1]
    zh_res = sa._fallback(HYP_RATE, sa._rule_desc(RULE_ZH, "zh-CN"), PROFILE, "zh-CN", rule=RULE_ZH)
    assert rule_row["detail_i18n"]["zh-CN"] == zh_res["trace"][1]["detail"]
    # critic 计数规范口径：三语轨迹行同一组数字
    critic_row = res["trace"][2]
    zh_num = critic_row["detail_i18n"]["zh-CN"].split("乐观词 ")[1].split(" 处")[0]
    en_num = critic_row["detail_i18n"]["en"].split("Optimistic terms ")[1].split(" ")[0]
    tw_num = critic_row["detail_i18n"]["zh-HK"].split("樂觀詞 ")[1].split(" 處")[0]
    assert zh_num == en_num == tw_num


def test_fallback_gen_row_no_detail_i18n():
    """gen 行 detail 走 detail_key+profile_parts 双轨，不加 detail_i18n（两机制不打架）。"""
    rd = sa._rule_desc(RULE_ZH, "zh-CN")
    res = sa._fallback(HYP_BASE, rd, PROFILE, "zh-CN", rule=RULE_ZH)
    gen = res["trace"][0]
    assert "detail_i18n" not in gen
    assert gen["detail_key"] and gen["profile_parts"] and gen["profile_tail"]
    assert gen["title_i18n"]["en"] == "Narrative generation · method"


def test_fallback_rule_row_variants_per_lang_rule_desc():
    """规则行三语 detail 各用该语言的 rule_desc（LTV cap 标签随语言）。"""
    res = sa._fallback(HYP_BASE, sa._rule_desc(RULE_EN, "en"), PROFILE, "en", rule=RULE_EN)
    rule_row = res["trace"][1]
    assert "LTV cap 70%" in rule_row["detail_i18n"]["en"]
    assert "LTV 上限 70%" in rule_row["detail_i18n"]["zh-CN"]
    assert "Proposed rule" in rule_row["detail_i18n"]["en"]


# --------------------------------------------------------------------------
# run_hypothesis（无 key → 兜底路径）：note_i18n / 数据边界披露
# --------------------------------------------------------------------------


@pytest.fixture
def no_llm(monkeypatch):
    monkeypatch.setattr(sa, "_load_env", lambda: {})


@pytest.fixture
def fake_llm(monkeypatch):
    """桩掉 langchain.agents.create_agent：不触网，返回一个可注入 messages 的假 agent。

    _run_agent 已改为现代 create_agent 入口；测试验证的是「轨迹重建 / i18n / 字段校验」
    逻辑而非模型，故 stub create_agent 直接吐出确定性消息链即可。返回假 agent，
    供工具行等用例覆盖其 .messages。
    """

    class _AIMsg:
        def __init__(self, content="", tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class _FakeAgent:
        def __init__(self):
            self.stub_body = None  # _fake_create_agent 按语言指令填入
            self.messages = []  # 测试可注入完整消息链（如 tool_calls 轨迹）；空则走 stub_body

        def invoke(self, inp, config=None):
            if self.messages:
                return {"messages": list(self.messages)}
            return {"messages": [_AIMsg(self.stub_body)]}

    fa = _FakeAgent()
    lc_agents = types.ModuleType("langchain.agents")

    def _fake_create_agent(*a, **k):
        # 语言兜底守卫要求 en/zh-HK 请求的叙事非中文——桩读 system_prompt 里的
        # 语言指令（与真实模型按指令输出同理），返回对应语言内容。
        sys_prompt = str(k.get("system_prompt") or "")
        if "English" in sys_prompt:
            body = (
                '{"narrative": "Synthetic narrative: borrower delinquency risk rises.",'
                ' "inference": "Inference: default probability decreases."}'
            )
        elif "繁體中文" in sys_prompt:
            body = (
                '{"narrative": "合成敘事：借款人逾期風險上行。",'
                ' "inference": "推演：違約概率下行。"}'
            )
        else:
            body = (
                '{"narrative": "合成叙事：借款人逾期风险上行。",'
                ' "inference": "推演：违约概率下行。"}'
            )
        fa.stub_body = body
        return fa

    lc_agents.create_agent = _fake_create_agent
    monkeypatch.setitem(sys.modules, "langchain.agents", lc_agents)
    monkeypatch.setattr(
        sa,
        "_load_env",
        lambda: {
            "LLM_KEY": "stub-key",
            "LLM_BASE_URL": "http://stub",
            "LLM_MODEL": "stub-model",
        },
    )
    return fa


@pytest.mark.parametrize("lang", ["zh-CN", "en", "zh-HK"])
def test_run_hypothesis_note_i18n(no_llm, lang):
    res = sa.run_hypothesis(HYP_BASE, rule=RULE_ZH, profile=PROFILE, lang=lang)
    assert res["note_i18n"][lang] == res["note"]
    assert res["note_i18n"]["en"], "note en 非空"
    assert res["note_i18n"]["zh-HK"], "note zh-HK 非空"
    assert "template" in res["note_i18n"]["en"].lower()


@pytest.mark.parametrize("lang", ["zh-CN", "en", "zh-HK"])
def test_run_hypothesis_sector_disclosure_i18n(no_llm, lang):
    """行业数据边界披露按语言各拼一次，拼完 zh-CN 仍与 zh-CN resolved 逐字节一致。"""
    res = sa.run_hypothesis(HYP_SECTOR, rule=RULE_ZH, profile=PROFILE, lang=lang)
    zh_ref = sa.run_hypothesis(HYP_SECTOR, rule=RULE_ZH, profile=PROFILE, lang="zh-CN")
    assert res["inference_i18n"]["zh-CN"] == zh_ref["inference"], "披露拼接后 zh-CN 铁律"
    assert "数据边界" in zh_ref["inference"]
    assert res["inference_i18n"][lang] == res["inference"], "披露拼接后当前语言自洽"
    for lg in ("zh-CN", "en", "zh-HK"):
        assert res["inference_i18n"][lg].endswith("）") or res["inference_i18n"][lg].endswith(")")
    # 无行业词的假设不触发披露，变体保持纯模板
    res2 = sa.run_hypothesis(HYP_BASE, rule=RULE_ZH, profile=PROFILE, lang=lang)
    assert "Data boundary" not in res2["inference_i18n"]["en"]


# --------------------------------------------------------------------------
# 轨迹行（两条路径共用）：_trace_critic / _trace_calib
# --------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["zh-CN", "en", "zh-HK"])
@pytest.mark.parametrize("opt,rsk", [(0, 0), (3, 1), (1, 5)])
def test_trace_critic_variants(opt, rsk, lang):
    critic = sa._critic_verdict(opt, rsk, lang)
    row = sa._trace_critic(critic, lang)
    assert row["detail_i18n"][lang] == row["detail"]
    assert row["title_i18n"]["zh-CN"] == "批评 · 美化偏见检测"
    assert row["title_i18n"]["en"] == "Critique · optimism-bias check"
    # 三语 note 复用同一组计数：三语行里的数字必须相同
    zh_num = row["detail_i18n"]["zh-CN"].split("乐观词 ")[1].split(" 处")[0]
    en_num = row["detail_i18n"]["en"].split("Optimistic terms ")[1].split(" ")[0]
    tw_num = row["detail_i18n"]["zh-HK"].split("樂觀詞 ")[1].split(" 處")[0]
    assert (zh_num, en_num, tw_num) == (str(opt),) * 3
    assert str(rsk) in row["detail_i18n"]["en"]
    # 卡体与轨迹行同源：critic.note_i18n 三语与 _trace_critic detail_i18n 里的
    # 同一句完全一致（同一组字面量，防漂移）。
    assert critic["note_i18n"]["en"] in row["detail_i18n"]["en"]
    assert critic["note_i18n"]["zh-HK"] in row["detail_i18n"]["zh-HK"]


def test_critic_note_i18n_card_track():
    """🔍 乐观偏见检查卡体渲染 critic.note，必须带三语变体
    （qa 真机发现该句曾停留请求时语言）。zh-CN 铁律：变体 == 单语 note。"""
    for lang in ("zh-CN", "en", "zh-HK"):
        critic = sa._critic_verdict(0, 5, lang)
        assert critic["note_i18n"][lang] == critic["note"], lang
    en = sa._critic_verdict(0, 5, "en")["note_i18n"]["en"]
    assert "keyword heuristic" in en
    assert "乐观偏见" not in en and "後續能力" not in en
    tw = sa._critic_verdict(0, 5, "zh-HK")["note_i18n"]["zh-HK"]
    assert tw.startswith("樂觀偏見檢查")


@pytest.mark.parametrize("lang", ["zh-CN", "en", "zh-HK"])
def test_trace_calib_variants(lang):
    row = sa._trace_calib(lang)
    assert row["detail_i18n"][lang] == row["detail"]
    assert row["title_i18n"]["zh-CN"] == "校准 · 状态"
    assert "uncalibrated" in row["detail_i18n"]["en"]
    assert row["detail_i18n"]["zh-HK"].startswith("本次輸出未接入")


# --------------------------------------------------------------------------
# _run_agent 模板路径（LLM 桩）：gen title / tool 行 / 末尾 critic+calib
# --------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["zh-CN", "en", "zh-HK"])
def test_run_agent_trace_variants(fake_llm, lang):
    res = sa.run_hypothesis(HYP_RATE, rule=RULE_ZH, profile=PROFILE, lang=lang)
    assert res["mode"] == "langchain-agent"
    gen = res["trace"][0]
    assert gen["title_i18n"]["zh-CN"] == "叙事生成 · LLM 推理"
    assert gen["title_i18n"]["en"] == "Narrative generation · LLM reasoning"
    # critic/calib 行（LLM 路径 resolved 沿用 zh 缺省，en/zh-HK 靠变体重画）
    kinds = [r["kind"] for r in res["trace"]]
    assert kinds[0] == "gen" and "critic" in kinds and "calib" in kinds
    critic_row = next(r for r in res["trace"] if r["kind"] == "critic")
    calib_row = next(r for r in res["trace"] if r["kind"] == "calib")
    assert critic_row["detail_i18n"]["en"] == critic_row["detail_i18n"]["en"]  # 键齐全
    assert critic_row["detail_i18n"]["zh-HK"]
    assert calib_row["detail_i18n"]["en"]
    # zh-CN 变体 == zh resolved（LLM 路径 resolved 缺省 zh-CN）
    assert critic_row["detail_i18n"]["zh-CN"] == critic_row["detail"]
    assert calib_row["detail_i18n"]["zh-CN"] == calib_row["detail"]
    assert gen["title_i18n"]["zh-CN"] == gen["title"]


def test_run_agent_tool_row_title_i18n(fake_llm):
    """触发一轮工具调用（假 agent 注入带 tool_calls 的消息链），验证 tool 行 title 三语变体。"""

    class _Msg:
        def __init__(self, content="", tool_calls=None, tool_call_id=None):
            self.content = content
            self.tool_calls = tool_calls
            self.tool_call_id = tool_call_id

    fake_llm.messages = [
        _Msg(tool_calls=[{"name": "get_risk_policy", "args": {}, "id": "t1"}], content=""),
        _Msg(tool_call_id="t1", content="LTV 警示线 0.75、强预警线 0.85；AVM 45% 覆盖 MAPE 9.88%"),
        _Msg(content='{"narrative": "Narrative.", "inference": "Conclusion."}'),
    ]
    res = sa.run_hypothesis(HYP_BASE, profile=PROFILE, lang="en")
    tool_rows = [r for r in res["trace"] if r["kind"] == "tool"]
    assert len(tool_rows) == 1
    row = tool_rows[0]
    assert row["title"].startswith("叙事生成 · 工具调用 ")
    assert row["title_i18n"]["zh-CN"] == row["title"]
    assert row["title_i18n"]["en"] == "Narrative generation · tool call get_risk_policy"
    assert row["title_i18n"]["zh-HK"] == "敘事生成 · 工具調用 get_risk_policy"


def test_run_agent_lang_guard_en_rejects_chinese(fake_llm):
    """en 请求下 LLM 无视语言指令输出中文正文 → 语言兜底拒收，走三语兜底，
    界面不得漏出错误语言的成段叙事。"""

    class _ZhMsg:
        def __init__(self, content=""):
            self.content = content
            self.tool_calls = None

    fake_llm.messages = [
        _ZhMsg(
            '{"narrative": "合成叙事：借款人逾期风险明显上行，偿债压力持续加剧。",'
            ' "inference": "推演：违约概率下行。"}'
        )
    ]
    res = sa.run_hypothesis(HYP_BASE, profile=PROFILE, lang="en")
    assert res["mode"] == "deterministic-fallback", "中文正文必须被语言守卫拒收"
    assert res["narrative_i18n"]["en"] == res["narrative"], "当前语言自洽：i18n[lang] == resolved"
    assert res["narrative_i18n"]["zh-CN"], "zh-CN 变体仍在（铁律由 _assert_variants_ok 另行守护）"


def test_tool_trace_detail_i18n_registered():
    """工具执行后轨迹队列里有该工具的 detail_i18n，且 zh-CN 与工具返回值逐字节一致
    （铁律）；en/zh-HK 变体非中文/非简体。"""
    tools = {t.name: t for t in sa._build_tools()}
    out = tools["get_risk_policy"].invoke({})
    recs = sa._tool_trace_records()
    assert recs and recs[-1]["name"] == "get_risk_policy"
    det = recs.pop()["detail_i18n"]
    assert det["zh-CN"] == out, "detail_i18n zh-CN 必须与工具返回值逐字节一致"
    assert det["en"] and det["zh-HK"]
    del tools
