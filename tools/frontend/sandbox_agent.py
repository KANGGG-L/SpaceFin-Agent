#!/usr/bin/env python
"""策略沙盒 · LangChain Agent + 工具调用 假设推演（精简版新增 AI 能力）。

把用户的一条「假设」（如"某社区消费贷利率下调 1%"）交给 LLM，
由模型自主决定是否调用风控计算工具（利率影响计算 / 风控策略参数 / 剧本节点），
再生成一段**合成借款人叙事 + 推演结论**，并用轻量 Critic 检测"美化偏见"，
强制打上 `synthetic / 未校准` 标签——延续原沙盒的诚实护栏。

LLM 后端 = OpenAI 兼容网关（base / model 由 .env 配置，未配置时取代码缺省）：
    LLM_BASE_URL   缺省复用 ai_guard.NL_LLM_DEFAULT_BASE（当前 https://api.mistral.ai/v1）
    LLM_KEY        用户提供（必填才能走真实 LLM）
    LLM_MODELS     逗号分隔回退链，首个为主；如 deepseek/deepseek-v4-flash-fast

工具（bind_tools + ReAct 式循环，纯确定性计算 / 项目真实参数，无外部知识库，非 RAG）：
    compute_loan_impact     利率变动对月供/收支比的示意影响
    get_risk_policy         当前 LTV 预警线与 AVM 精度口径
    get_scenario_calendar   7 天风险演进剧本关键节点

**健壮性**：无 key 或 langchain 未安装，或真实 LLM 调用失败 → 一律降级为
确定性兜底生成器，保证离线可跑、页面永不 500。这本文件就是「AI PM 用
LangChain 把假设变成推演叙事」的最小可讲单元，注释即讲解。
"""

import hashlib
import json
import os
import re
import threading

# 仓库根：本文件在 <root>/tools/frontend/ 下，回退三层。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 共享守卫原语（0 环：sandbox_agent 在 tools/frontend 根，ai_guard 为 0 依赖 stdlib 叶模块）：
# 注入检测 / 输入清洗 / 读超时与总预算常量。
import ai_guard

# 单次推演的总耗时预算（秒）：对齐 ai_guard.NL_LLM_TOTAL_BUDGET 的语义但沙盘更大——
# ReAct 循环 + 工具调用可能多轮，不能把同步 handler 拖到几十秒。
SAND_AGENT_TOTAL_BUDGET = 60  # 对齐 ai_guard.NL_LLM_TOTAL_BUDGET 的语义但沙盘更大——ReAct 循环可能多轮 llm.invoke + 工具调用，单轮时延可达十余秒，预算过小会被掐断


def _load_env():
    """只读仓库根 .env（与 tools/lake 同逻辑，读不写）。"""
    env = {}
    path = os.path.join(_REPO_ROOT, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def _llm_config(env):
    # 统一键：LLM_KEY / LLM_BASE_URL / LLM_MODELS（以 .env 为准）。
    key = _kv(env, "LLM_KEY")
    base = _kv(env, "LLM_BASE_URL", default=ai_guard.NL_LLM_DEFAULT_BASE)
    # 模型：优先 LLM_MODELS（逗号分隔取第一个），回退 LLM_MODEL 单值，再无则默认。
    chain = [m for m in _kv(env, "LLM_MODELS").split(",") if m.strip()]
    if not chain:
        single = _kv(env, "LLM_MODEL")
        chain = [single] if single else [ai_guard.NL_LLM_DEFAULT_MODEL]
    return key, base, chain[0]


def _kv(env, *names, default=""):
    """按序取 .env 键，命中第一个非空即返回。"""
    for n in names:
        v = str((env or {}).get(n, "")).strip()
        if v:
            return v
    return default


def _rule_desc(rule, lang="zh-CN"):
    """把前端传来的拟议规则压成一句描述；无有效内容返回 None。

    rule 形如 {"scope_label": "...", "action_label": "拒贷", "ltv_cap": 0.7}。
    lang 决定「LTV 上限 / LTV cap」这类标签的语种：整段叙事是 lang 语，
    标签不跟着走就会中英混排（英文页面里冒出「… · LTV 上限 70%」）。
    """
    if not isinstance(rule, dict):
        return None
    parts = []
    scope = rule.get("scope_label") or rule.get("scope_value")
    if scope:
        parts.append(str(scope))
    act = rule.get("action_label") or rule.get("action")
    if act:
        parts.append(str(act))
    cap = rule.get("ltv_cap")
    if cap is not None:
        try:
            # 口径：ltv_cap 全链路是小数（0.7），而页面展示一律百分比（70.00%）。
            # 叙事正文里写「0.7」会跟同屏的结果区（70.00%）自相矛盾，统一按百分比拼。
            # 标签随语言走（en 页同屏其他位置写的是 "LTV cap"，不本地化就是中英混排）。
            parts.append(f"{_loc(lang, 'LTV 上限', 'LTV cap')} {float(cap) * 100:g}%")
        except (TypeError, ValueError):
            pass
    return " · ".join(parts) if parts else None


# 叙事语言：默认中文；前端切到 en / zh-HK 时透传，让 LLM 直接按目标语言生成。
_LANG_NAME = {"en": "English", "zh-HK": "繁體中文"}


def _ln(lang):
    return _LANG_NAME.get(lang, "中文")


def _loc(lang, zh, en, tw=None):
    """按 lang 取对应文案；zh-HK 缺省回落简体。

    缺省回落只适用于**简繁同形的短标签/术语**（如「LTV 上限」「未校准」）。
    成段的正文（叙事、推演结论、轨迹说明）必须显式给 tw=：简体整段直出在繁體
    界面里是繁简混排，比同形的标签显眼得多（见 _fallback 各处 tw= 译文）。
    """
    if lang == "en":
        return en
    if lang == "zh-HK":
        return tw if tw else zh
    return zh


# 三语变体统一走这三个键（与前端 window.__LANG__ 取值一致）。
_I18N_LANGS = ("zh-CN", "en", "zh-HK")


def _loc3(zh, en, tw):
    """三语变体 map：与 _loc 同一组字面量打包成 {lang: 文案} 随 payload 下发，
    前端按当前语言整句重画——避免把几十个模板复制进 JS 字典（双份维护、拼 key 易漂移）。
    铁律：i18n["zh-CN"] 必须与 lang="zh-CN" 时的 resolved 输出逐字节一致（测试断言）。
    tw 即 zh-HK 变体（与 _loc 第三参同义）；调用点必须显式给全三语。"""
    return {"zh-CN": zh, "en": en, "zh-HK": tw}


def run_hypothesis(hypothesis, rule=None, profile=None, lang="zh-CN", decompose=None):
    """策略沙盒入口：假设（+ 可选拟议规则、模拟对象）→ 推演结果 dict。

    rule 是合并页 B 区的拟议规则（压力背景下的评估对象）：传入后推演结论
    围绕「该情景下这条规则是否足够、缺口在哪」生成，而不是孤立地聊情景——
    这是「情景假设与拟议规则真正打通」的叙事腿。
    profile 是作用域内选出的**真实贷款**（p6_policy.pick_profile_loan）：
    叙事以该笔贷款的借款人为原型，而非凭空编造与组合无关的画像。
    lang 透传前端当前语言（zh-CN / en / zh-HK），决定叙事生成语言。
    decompose 是 P6「运行推演」第二段确认后的 AI 多步推理产物
    （{"steps":[…], "conditions":{…}}，conditions 里的 direction/shock_text/scope/pct
    是用户核对过的结构化条件）：推理链前插进 trace，条件并入提示让推理围绕它展开；
    conditions.inferred 为真时结论固定追加「推断假设非观测」披露。

    返回结构（前端直接渲染）：
        hypothesis / narrative / inference / synthetic / calibration_status /
        critic_flag / model / provider / mode / note / trace / llm_error(可选)
        另附三语变体：narrative_i18n / inference_i18n / note_i18n（兜底路径），
        trace 各行附 title_i18n / detail_i18n——前端按当前语言整句重画，
        变体 ["zh-CN"] 与 lang="zh-CN" 时的 resolved 逐字节一致（铁律，测试断言）。
    """
    hypothesis = (hypothesis or "").strip()
    if not hypothesis:
        raise ValueError("hypothesis 不能为空")
    # 硬伤②：假设未过注入检测 → 绝不送模型，直接确定性兜底并三语明示。
    # （ai_guard._looks_like_injection 与原 p6 NL 守卫同一条注入模式表）
    if ai_guard._looks_like_injection(hypothesis):
        result = _fallback(
            hypothesis, _rule_desc(rule, lang), profile, lang, rule=rule, decompose=decompose
        )
        inj_zh = "疑似指令注入，未送模型，已使用确定性兜底。"
        inj_en = "Suspected prompt injection; the hypothesis was not sent to a model; deterministic fallback used."
        inj_tw = "疑似指令注入，未送模型，已使用確定性兜底。"
        result["note"] = _loc(lang, inj_zh, inj_en, inj_tw)
        result["note_i18n"] = _loc3(inj_zh, inj_en, inj_tw)
        result["llm_error"] = "injection-blocked"
        return result
    rule_desc = _rule_desc(rule, lang)
    profile_desc = _profile_desc(profile if isinstance(profile, dict) else None)
    profile_tracks = _profile_desc_tracks(profile if isinstance(profile, dict) else None)

    key, base, model = _llm_config(_load_env())

    if key:
        try:
            result = _run_agent(
                hypothesis,
                key,
                base,
                model,
                rule_desc,
                profile_desc,
                lang,
                profile_tracks,
                decompose,
            )
        except Exception as exc:  # 真实 LLM 失败不崩页面：降级兜底并标注
            result = _fallback(hypothesis, rule_desc, profile, lang, rule=rule, decompose=decompose)
            result["llm_error"] = str(exc)[
                :200
            ]  # 限长：保留本地诊断，防上游异常把 URL/主机名灌满/泄漏
            note_zh = "真实 LLM 调用失败，已降级为确定性兜底。"
            note_en = "Real LLM call failed; fell back to a deterministic narrative."
            note_tw = "真實 LLM 呼叫失敗，已降級為確定性兜底。"
            result["note"] = _loc(lang, note_zh, note_en, note_tw)
            result["note_i18n"] = _loc3(note_zh, note_en, note_tw)
    else:
        result = _fallback(hypothesis, rule_desc, profile, lang, rule=rule, decompose=decompose)
        note_zh = "本页叙事由内置规则模板生成，未接入大模型，结果可复现、不代表真实 AI 推理。"
        note_en = (
            "This narrative is generated by a built-in rule template without a large model; "
            "results are reproducible and do not represent real AI reasoning."
        )
        note_tw = "本頁敘事由內置規則模板生成，未接入大模型，結果可複現、不代表真實 AI 推理。"
        result["note"] = _loc(lang, note_zh, note_en, note_tw)
        result["note_i18n"] = _loc3(note_zh, note_en, note_tw)

    # 行业定向的数据边界：情景点名某行业、而组合数据没有行业字段时必须披露——
    # 否则用户会误以为叙事真的筛选过「制造业借款人」。
    sector = _SECTOR_RE.search(hypothesis)
    if sector and isinstance(profile, dict) and profile.get("loan_id"):
        sn = sector.group(1)
        discs = {
            "zh-CN": (
                f"（数据边界：组合客户主档不含行业字段，无法定向筛选「{sn}」相关借款人；"
                "行业暴露需在 customer 表补充 industry 字段后方可定量）"
            ),
            "en": (
                f"(Data boundary: the portfolio customer master has no industry field, so borrowers "
                f"related to '{sn}' cannot be targeted; industry exposure requires adding "
                f"an industry field to the customer table before it can be quantified)"
            ),
            "zh-HK": (
                f"（資料邊界：組合客戶主檔不含行業欄位，無法定向篩選「{sn}」相關借款人；"
                "行業暴露需在 customer 表補充 industry 欄位後方可定量）"
            ),
        }
        result["inference"] = (result.get("inference") or "") + discs.get(lang, discs["zh-CN"])
        if isinstance(result.get("inference_i18n"), dict):
            # 数据边界披露按语言各拼一次：zh-CN 变体拼 discs["zh-CN"] 后仍与
            # resolved（lang="zh-CN"）逐字节一致。
            for lg in _I18N_LANGS:
                result["inference_i18n"][lg] = (result["inference_i18n"].get(lg) or "") + discs[lg]

    # decompose 的 conditions.inferred 为真 → 结论固定追加「推断假设非观测」披露
    # （复刻上面 _SECTOR_RE 披露模式：resolved 与 *_i18n 三语各拼一次）。
    if (
        decompose
        and isinstance(decompose, dict)
        and (decompose.get("conditions") or {}).get("inferred")
    ):
        disc_inf = {
            "zh-CN": "（推断假设：该传导为模型推断，非数据观测，不构成授信决策依据）",
            "en": "(Inferred assumption: this transmission is model-inferred, not observed data, "
            "and is not a basis for credit decisions)",
            "zh-HK": "（推斷假設：該傳導為模型推斷，非數據觀測，不構成授信決策依據）",
        }
        result["inference"] = (result.get("inference") or "") + disc_inf.get(
            lang, disc_inf["zh-CN"]
        )
        if isinstance(result.get("inference_i18n"), dict):
            for lg in _I18N_LANGS:
                result["inference_i18n"][lg] = (result["inference_i18n"].get(lg) or "") + disc_inf[
                    lg
                ]
    return result


# --------------------------------------------------------------------------
# 真实 LLM 路径（LangChain Agent + 工具调用）
# --------------------------------------------------------------------------

# 工具调用的三语轨迹记录队列（thread-local：并发请求互不串线）。
# 工具执行时按调用顺序 append，trace 重建时按同名 FIFO 消费——同一工具可能被
# 调用多次，所以按名字排队而不是单槽覆盖。
_TOOL_TRACE = threading.local()


def _tool_trace_records():
    recs = getattr(_TOOL_TRACE, "records", None)
    if recs is None:
        recs = _TOOL_TRACE.records = []
    return recs


def _pop_tool_detail_i18n(name):
    """取该工具名最早一次调用的 detail_i18n（FIFO）；无记录返回 None，
    前端 narrLangPick 自动回落 detail 原文（zh-CN），行为向后兼容。"""
    recs = _tool_trace_records()
    for i, r in enumerate(recs):
        if r["name"] == name:
            return recs.pop(i)["detail_i18n"]
    return None


def _build_tools():
    """定义 Agent 可调用的工具（纯确定性计算 / 项目真实参数，无外部知识库，非 RAG）。"""
    from langchain_core.tools import tool

    @tool
    def compute_loan_impact(rate_change_pct: float, base_rate_pct: float = 4.2) -> str:
        """计算贷款利率变动对合成借款人月供与收支比的示意影响。
        rate_change_pct: 利率变动百分点，如 -0.5 表示下调 0.5 个百分点；
        base_rate_pct: 当前基准利率，默认 4.2%。"""
        principal, n = 1_000_000.0, 360
        r0 = base_rate_pct / 100 / 12
        r1 = (base_rate_pct + rate_change_pct) / 100 / 12
        pmt0 = principal * r0 * (1 + r0) ** n / ((1 + r0) ** n - 1)
        pmt1 = principal * r1 * (1 + r1) ** n / ((1 + r1) ** n - 1)
        delta = pmt1 - pmt0
        down = delta < 0
        zh = (
            f"贷款100万/30年：月供由 ¥{pmt0:,.0f} 变为 ¥{pmt1:,.0f}，"
            f"每月变动 ¥{delta:,.0f}（{'下降' if down else '上升'}）。"
        )
        # 工具返回值（喂 LLM）保持中文；轨迹行另发三语 detail_i18n，en/zh-HK 界面
        # 的「工具调用」展开不再漏中文。同参数拼三语，zh-CN 与返回值逐字节一致。
        _tool_trace_records().append(
            {
                "name": "compute_loan_impact",
                "detail_i18n": _loc3(
                    zh,
                    f"Loan 1,000,000 / 30 years: monthly payment changes from ¥{pmt0:,.0f} to ¥{pmt1:,.0f}, "
                    f"a monthly change of ¥{delta:,.0f} ({'decrease' if down else 'increase'}).",
                    f"貸款100萬/30年：月供由 ¥{pmt0:,.0f} 變為 ¥{pmt1:,.0f}，"
                    f"每月變動 ¥{delta:,.0f}（{'下降' if down else '上升'}）。",
                ),
            }
        )
        return zh

    @tool
    def get_risk_policy() -> str:
        """返回当前风控策略参数：LTV 两档预警线与 AVM 精度口径。"""
        zh = (
            "LTV 警示线 0.75、强预警线 0.85（等号边界严格大于不触发）；"
            "AVM 高置信子集 45% 覆盖 MAPE 9.94%，全量 15.00%，基线 20.58%（当前发布模型 2026-08-20-r2）；"
            "预警推送为 T+1 批处理。"
        )
        _tool_trace_records().append(
            {
                "name": "get_risk_policy",
                "detail_i18n": _loc3(
                    zh,
                    "LTV warning line 0.75, strong warning line 0.85 (strictly greater-than; equality does not trigger); "
                    "AVM high-confidence subset covers 45% with MAPE 9.94%, full set 15.00%, baseline 20.58% (current release model 2026-08-20-r2); "
                    "alerts are pushed via T+1 batch processing.",
                    "LTV 警示線 0.75、強預警線 0.85（等號邊界嚴格大於不觸發）；"
                    "AVM 高信心子集 45% 覆蓋 MAPE 9.94%，全量 15.00%，基線 20.58%（當前發布模型 2026-08-20-r2）；"
                    "預警推送為 T+1 批次處理。",
                ),
            }
        )
        return zh

    @tool
    def get_scenario_calendar() -> str:
        """返回 7 天风险演进剧本关键节点（演示用，非真实市场判断）。"""
        zh = "D4 广州核心区挂牌价异动下探 → D5 LTV 预警达峰 → D7 企稳收口；全链路为演示回填。"
        _tool_trace_records().append(
            {
                "name": "get_scenario_calendar",
                "detail_i18n": _loc3(
                    zh,
                    "D4 listing prices in Guangzhou's core areas dip → D5 LTV warnings peak → D7 stabilization; "
                    "the whole chain is demo backfill.",
                    "D4 廣州核心區掛牌價異動下探 → D5 LTV 預警達峰 → D7 企穩收口；"
                    "全鏈路為演示回填。",
                ),
            }
        )
        return zh

    return [compute_loan_impact, get_risk_policy, get_scenario_calendar]


def _validate_agent_fields(parsed, lang):
    """硬伤③：narrative/inference 逐字段校验——非空 str、经 _sanitize_nl_text 清洗、
    长度下限（≥2，空/单字符垃圾一律拒收）。任一不合法 → 返回 None（调用方 raise →
    run_hypothesis 走 _fallback）。

    模型原文（raw）绝不进任何展示位：校验不过就不产出该字段，绝不用原文兜底。

    语言兜底：en 请求下 narrative/inference 中文占比过高（>30%）视为模型无视了
    语言指令 → 整体拒收走 _fallback（其 narrative_i18n/inference_i18n 三语齐备，
    界面不会漏出错误语言的成段正文）。zh-HK 本就是中文（繁体），简繁之别由
    critic 词表与文案本身保证，不做比例检查；zh-CN 数字/专名含英文正常，同样不查。
    """
    if not isinstance(parsed, dict):
        return None
    out = {}
    for k in ("narrative", "inference"):
        v = parsed.get(k)
        if not isinstance(v, str):
            return None
        c = ai_guard._sanitize_nl_text(v)[0].strip()
        if len(c) < 2:
            return None
        if lang == "en" and c:
            cjk = sum(1 for ch in c if "\u4e00" <= ch <= "\u9fff")
            if cjk / len(c) > 0.3:
                return None
        out[k] = c
    return out


def _decompose_cond_desc(conds, lang):
    """把合规的 decompose conditions 压成一句人话描述（供 prompt / 兜底模板）。"""
    if not isinstance(conds, dict):
        return None
    parts = []
    d = conds.get("direction")
    if d == "up":
        parts.append(_loc(lang, "上行（脱险）", "Upward (de-risk)", "上行（脫險）"))
    elif d == "down":
        parts.append(_loc(lang, "下行（新增命中）", "Downward (new hits)", "下行（新增命中）"))
    sh = conds.get("shock_text")
    if sh:
        parts.append(str(sh))
    if conds.get("scope_value"):
        parts.append(f"{_loc(lang, '作用域', 'scope')}={conds['scope_value']}")
    pct = conds.get("pct")
    if pct is not None:
        try:
            parts.append(f"{_loc(lang, 'pct', 'pct')}={float(pct):+.2f}")
        except (TypeError, ValueError):
            pass
    return "；".join(parts) if parts else None


def _decompose_trace_pre(steps):
    """把 decompose 的推理链 steps 组装成 trace 前插行（kind="decompose"，三语齐备）。

    _run_agent（真实 LLM）与 _fallback（确定性兜底）共用同一份组装：规格 D.2
    要求 decompose 的 steps 前插进 trace 的 kind:decompose 行，兜底路径同样如实
    呈现推理链。steps 里若缺 title_i18n/detail_i18n，用 title/detail 单语兜底
    （_loc3 补齐三语键；与 _run_agent 原内联逻辑逐字节等价）。
    """
    pre = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        t_i18n = (
            s.get("title_i18n")
            if isinstance(s.get("title_i18n"), dict)
            else _loc3(s.get("title", ""), s.get("title_key", ""), s.get("title_key", ""))
        )
        d_i18n = (
            s.get("detail_i18n")
            if isinstance(s.get("detail_i18n"), dict)
            else _loc3(s.get("detail", ""), s.get("detail_key", ""), s.get("detail_key", ""))
        )
        pre.append(
            {
                "kind": "decompose",
                "title": s.get("title", ""),
                "title_i18n": t_i18n,
                "detail": s.get("detail", ""),
                "detail_i18n": d_i18n,
            }
        )
    return pre


def _run_agent(
    hypothesis,
    key,
    base,
    model,
    rule_desc=None,
    profile_desc=None,
    lang="zh-CN",
    profile_tracks=None,
    decompose=None,
):
    # 仅在真正调用时才 import langchain，避免无依赖时整页 500。
    # 现代 langchain（1.x）统一用 langchain.agents.create_agent：传入 BaseChatModel + tools，
    # 由 langgraph 内部驱动 tool-calling/ReAct 循环。Mistral 仅提供 OpenAI 兼容端点，故用
    # ChatOpenAI；对应旧手写循环(ai.tool_calls/ToolMessage)在此由 create_agent 接管。
    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI

    tools = _build_tools()
    # ChatOpenAI 在 base 后**再拼 /chat/completions**；base 配置（如
    # https://api.mistral.ai/v1）已带 /v1，直接传即可合成合规的 /v1/chat/completions。
    # 单步读超时 + 禁无限重试（复用 ai_guard 的读超时常量）。工具绑定由 create_agent 内部完成。
    _llm = ChatOpenAI(
        model=model,
        api_key=key,
        base_url=base,
        temperature=0.3,
        max_tokens=1500,
        timeout=ai_guard.NL_LLM_READ_TIMEOUT,
        max_retries=1,
    )

    system = (
        "你是房产金融风控的策略推演助手。基于用户假设，按需调用工具获取量化依据，"
        "再生成合成借款人叙事与推演结论。必须客观，不得美化借款人风险"
        "（不得出现'努力''积极''希望'等带有社会期望偏见的措辞）。"
    )
    if rule_desc:
        system += (
            f"用户的评估对象是拟议规则（{rule_desc}）：inference 必须回答"
            "「该假设情景下这条拟议规则是否足以覆盖风险、残余缺口在哪里」，"
            "而不是孤立地评价情景本身。"
        )
    if profile_desc:
        system += (
            f"合成借款人必须以该笔真实贷款的借款人为原型：{profile_desc}。"
            "叙事围绕其真实负债结构展开，不得另行虚构收入或负债。"
        )
    cond_desc = _decompose_cond_desc((decompose or {}).get("conditions"), lang)
    if cond_desc:
        # 用户已确认的分解冲击条件（P6 第二段产物）：推理必须围绕该条件展开
        system += (
            f"用户还给出了已确认的分解冲击条件（{cond_desc}）：推理应围绕该条件的"
            "影响方向展开，叙事与结论不得脱离该条件另起炉灶。"
        )
    ln = _ln(lang)
    system += (
        f"请使用{ln}输出所有内容。最后只输出一个 JSON 对象，字段："
        f"narrative=合成借款人叙事（{ln}，2-3 句），"
        f"inference=推演结论（{ln}，说明该假设对违约/行为的可能影响，1-2 句）。"
    )
    human = f"假设：{hypothesis}\n"
    if cond_desc:
        human += f"已确认的分解冲击条件：{cond_desc}\n"
    if profile_desc:
        human += f"模拟对象（作用域内真实贷款）：{profile_desc}\n"
    if rule_desc:
        human += f"拟议规则（评估对象）：{rule_desc}\n"
    human += "请推理并只输出 JSON。"
    # create_agent 经 system_prompt=system 注入系统提示，输入只用 human 一条。

    _tool_trace_records().clear()  # 防上一轮/上一请求残留的轨迹记录串行
    called_tools = []
    gen_entry = {
        "kind": "gen",
        "title": "叙事生成 · LLM 推理",
        "title_i18n": _loc3(
            "叙事生成 · LLM 推理", "Narrative generation · LLM reasoning", "敘事生成 · LLM 推理"
        ),
        "detail": f"模型 {model}（temperature 0.3）；按需调用风控工具获取量化依据后生成叙事与结论"
        + (f"；评估对象：拟议规则「{rule_desc}」" if rule_desc else "")
        + (f"；模拟对象：{profile_desc}" if profile_desc else ""),
    }
    if profile_tracks:
        # 双轨：detail 是中文回落原句；detail_key + profile_parts/tail 供前端按当前
        # 语言重画（zh-CN 的渲染结果与 detail 逐字一致，见 policy 页字典注册）。
        _, parts, tail = profile_tracks
        gen_entry["detail_key"] = (
            "模型 {model}（temperature 0.3）；按需调用风控工具获取量化依据后生成叙事与结论"
            + ("；评估对象：拟议规则「{rule}」" if rule_desc else "")
            + "；模拟对象：{profile}"
        )
        gen_entry["detail_params"] = {"model": model}
        if rule_desc:
            gen_entry["detail_params"]["rule"] = rule_desc
        gen_entry["profile_parts"] = parts
        gen_entry["profile_tail"] = tail
    trace = [gen_entry]
    # 推理链前插：decompose.steps 在 gen_entry 之前（kind="decompose"，三语齐备）
    if decompose and isinstance(decompose, dict) and decompose.get("steps"):
        trace = _decompose_trace_pre(decompose["steps"]) + trace
    # 现代统一入口：create_agent 把 tool-calling/ReAct 循环内聚由 langgraph 驱动。
    agent = create_agent(model=_llm, tools=tools, system_prompt=system, name="spacefin_sandbox")
    # 总预算硬截止：整个 agent 跑在 worker 线程，future.result(timeout) 掐总耗时；
    # 超时/任何异常统一抛 TimeoutError → run_hypothesis 的 try/except 走 _fallback。
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(agent.invoke, {"messages": [HumanMessage(content=human)]})
        try:
            result = fut.result(timeout=SAND_AGENT_TOTAL_BUDGET)
        except Exception as err:  # noqa: BLE001 - TimeoutError 或下级异常 → 统一兜底
            raise TimeoutError("agent total budget exceeded") from err

    msgs = list((result or {}).get("messages") or [])
    # 逐轮审计轨迹：抓每条带 tool_calls 的 assistant 消息 → 生成 kind:"tool" 行；
    # 对应 ToolMessage 的 content 作 detail。
    called_tools = []
    tool_result = {
        getattr(m, "tool_call_id", None): m.content
        for m in msgs
        if getattr(m, "tool_call_id", None) is not None
    }
    for m in msgs:
        for tc in getattr(m, "tool_calls", None) or []:
            name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            called_tools.append(name)
            trace.append(
                {
                    "kind": "tool",
                    "title": f"叙事生成 · 工具调用 {name}",
                    "title_i18n": _loc3(
                        f"叙事生成 · 工具调用 {name}",
                        f"Narrative generation · tool call {name}",
                        f"敘事生成 · 工具調用 {name}",
                    ),
                    "detail": str(
                        tool_result.get(
                            tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", ""), ""
                        )
                    ),
                    "detail_i18n": _pop_tool_detail_i18n(name),
                }
            )
    # 终态：最后一次无 tool_calls 的 assistant 消息的 content（Anthropic 可能是片段数组）。
    finals = [m for m in msgs if not getattr(m, "tool_calls", None)]
    raw = (finals[-1].content if finals else "") or ""
    if isinstance(raw, list):
        raw = "".join((b.get("text", "") if isinstance(b, dict) else str(b)) for b in raw)
    # 上游信任边界：模型原文（不均匀校准）在用前截到底线，防恶意上游吐超大响应拖垮
    # _extract_json 的正则/内存；narrative/inference 后续另有 _validate_agent_fields 洗到 200。
    if len(raw) > ai_guard.NL_LLM_RAW_MAX_CHARS:
        raw = raw[: ai_guard.NL_LLM_RAW_MAX_CHARS]
    parsed = _extract_json(raw)
    # 硬伤③：逐字段校验，绝不拿模型原文兜底（校验不过 → raise → run_hypothesis 走 _fallback）
    valid = _validate_agent_fields(parsed, lang)
    if valid is None:
        raise ValueError("narrative/inference 校验未过")
    narrative = valid["narrative"]
    inference = valid["inference"]

    critic = _critic(narrative + " " + inference, lang)
    trace.append(_trace_critic(critic))
    trace.append(_trace_calib())
    return {
        "hypothesis": hypothesis,
        "narrative": narrative,
        "inference": inference,
        "tools_called": called_tools,
        "synthetic": True,
        "calibration_status": "未校准",
        "critic_flag": critic,
        "model": model,
        "provider": "openai",
        "mode": "langchain-agent",
        "trace": trace,
    }


def _extract_json(text):
    """从 LLM 输出里尽量抠出 JSON 对象（容错：可能带 markdown 代码块）。"""
    text = (text or "").strip()
    if not text:
        return {}
    # 去 ```json ... ``` 围栏
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {}


# --------------------------------------------------------------------------
# Critic：检测"美化偏见"（轻量启发式，非模型）
# --------------------------------------------------------------------------

# 繁体形态必须各写一份：译文走香港繁体后（乐观→樂觀、违约→違約），
# 只留简体关键词会让 zh-HK 叙事的命中数恒为 0——Critic 在繁体界面下静默失效。
# 简繁同形的词（逾期 / 下行 / 努力 / 希望）不必重复。
_OPTIMISTIC = [
    "乐观",
    "积极",
    "努力",
    "希望",
    "奋斗",
    "向好",
    "稳健",
    "前景广阔",
    "充满信心",
    "樂觀",
    "積極",
    "奮鬥",
    "穩健",
    "前景廣闊",
    "充滿信心",
    "optimistic",
    "hopeful",
    "resilient",
    "promising",
    "confident",
    "doing well",
]
_RISK = [
    "违约",
    "逾期",
    "断供",
    "风险",
    "下行",
    "压力",
    "失业",
    "资不抵债",
    "恶化",
    "降级",
    "違約",
    "斷供",
    "風險",
    "壓力",
    "失業",
    "資不抵債",
    "惡化",
    "降級",
    "default",
    "delinquent",
    "risk",
    "downward",
    "pressure",
    "unemploy",
    "insolvent",
    "deteriorat",
    "downgrade",
    "shortfall",
]


# 关键词大小写不敏感：英文叙事混合大小写，先归一化再计数。
def _critic_counts(text):
    """乐观/风险词命中计数。只算一次、三语 note 复用同一组数字——
    保证三语轨迹行数字一致、可复现（en/zh-HK 文本各算会各得一套数）。"""
    t = (text or "").lower()
    opt = sum(t.count(w.lower()) for w in _OPTIMISTIC)
    rsk = sum(t.count(w.lower()) for w in _RISK)
    return opt, rsk


def _critic_verdict(opt, rsk, lang="zh-CN"):
    """按同一组计数产出各语言的判定与说明（词表本身三语齐备，判定条件与语言无关）。"""
    biased = opt >= 2 and opt > rsk
    flag = _loc(
        lang,
        "疑似美化偏见" if biased else "未见明显美化偏见",
        "Suspected optimism bias" if biased else "No obvious optimism bias",
        "疑似美化偏見" if biased else "未見明顯美化偏見",
    )
    # note 字面量单一来源：卡体（🔍 乐观偏见检查）与轨迹行（_trace_critic 的
    # detail_i18n）共用同一组句子，按语言重画不漂移。note_i18n["zh-CN"] 与
    # lang="zh-CN" 时的 note 逐字节一致（zh-CN 零回归铁律）。
    note_zh = "乐观偏见检查仅做关键词启发式检测；对照真实基准的量化校准属后续能力。"
    note_en = (
        "The optimism-bias check is a keyword heuristic only; quantitative calibration "
        "against a real baseline is a follow-up capability."
    )
    note_tw = "樂觀偏見檢查僅做關鍵詞啟發式檢測；對照真實基準的量化校準屬後續能力。"
    note = _loc(lang, note_zh, note_en, note_tw)
    return {
        "optimistic_hits": opt,
        "risk_hits": rsk,
        "flag": flag,
        "note": note,
        "note_i18n": _loc3(note_zh, note_en, note_tw),
    }


def _critic(text, lang="zh-CN"):
    opt, rsk = _critic_counts(text)
    return _critic_verdict(opt, rsk, lang)


# 情景中的行业词：用于判定「情景是否定向到某行业客群」。
# 组合数据（customer 表）不含行业/职业字段——命中行业词时必须诚实披露数据边界。
_SECTOR_RE = re.compile(r"(制造业|互联网|零售|服务业|外贸|房地产|金融|教育|医疗|物流|科技)")


def _profile_desc_tracks(profile):
    """模拟对象事实骨架的双轨拆分：(zh 整句, parts, tail)；无模拟对象返回 None。

    zh 整句与 _profile_desc 的历史输出逐字节一致（prompt / 兜底回落原句共用）；
    parts/tail 供前端按当前语言逐段 t() 后拼接——整体模板无法覆盖可选字段
    （城市/LTV/收入/偿债比/分类都可能缺），拆段拼接的分隔符「 · 」与 zh 原句
    逐字节一致。数字一律预格式化为字符串（前端 t() 只做 {name} 简单替换）；
    城市段 params 传城市码，前端 i18nParams 经 CITY_CODES 翻中文名再 t() 译出。
    """
    if not isinstance(profile, dict) or not profile.get("loan_id"):
        return None
    zh_bits = [f"贷款 #{profile['loan_id']}"]
    parts = [{"k": "贷款 #{loan_id}", "p": {"loan_id": str(profile["loan_id"])}}]
    if profile.get("city_name"):
        zh_bits.append(profile["city_name"])
        # params 传城市码：zh-CN 兜底整句仍用 city_name 中文名（历史口径）。
        parts.append({"k": "{city}", "p": {"city": profile.get("city") or profile["city_name"]}})
    balance = f"{profile.get('balance', 0):,.0f}"
    zh_bits.append(f"余额 {balance} 元")
    parts.append({"k": "余额 {balance} 元", "p": {"balance": balance}})
    if profile.get("ltv") is not None:
        # 口径同 _rule_desc：LTV 存小数、展示统一百分比（与结果区 fmtLtv 一致）。
        ltv = f"{profile['ltv'] * 100:.2f}%"
        zh_bits.append(f"LTV {ltv}")
        parts.append({"k": "LTV {ltv}", "p": {"ltv": ltv}})
    if profile.get("income_monthly"):
        income = f"{profile['income_monthly']:,.0f}"
        zh_bits.append(f"月收入 {income} 元")
        parts.append({"k": "月收入 {income} 元", "p": {"income": income}})
    if profile.get("debt_ratio") is not None:
        debt = f"{profile['debt_ratio']:.2f}"
        zh_bits.append(f"偿债比 {debt}")
        parts.append({"k": "偿债比 {debt_ratio}", "p": {"debt_ratio": debt}})
    if profile.get("risk_class"):
        # risk_class 是五级分类有限枚举（正常/关注/次级/可疑/损失），
        # 枚举值本身已在 en/zh-HK 字典注册，i18nParams 逐值 t() 译出。
        zh_bits.append(f"分类「{profile['risk_class']}」")
        parts.append({"k": "分类「{risk_class}」", "p": {"risk_class": profile["risk_class"]}})
    hit = "（拟议规则命中笔，选取口径：命中笔中 LTV 最高）"
    if not profile.get("is_hit"):
        hit = "（作用域内在贷笔，选取口径：命中笔中 LTV 最高）"
    return " · ".join(zh_bits) + hit, parts, {"k": hit}


def _profile_desc(profile):
    """把模拟对象（作用域内真实贷款）压成一句事实描述，供 prompt / 兜底叙事共用。

    展示给用户的叙事轨迹走双轨（_profile_desc_tracks）；本函数保留整句中文，
    供喂给叙事模型的 prompt 上下文（内部不展示）与回落原句使用。"""
    tracks = _profile_desc_tracks(profile)
    return tracks[0] if tracks else None


def _trace_critic(critic, lang="zh-CN"):
    """批评步骤轨迹。lang 缺省 zh-CN：真实 LLM 路径（_run_agent）未本地化轨迹，
    沿用改动前的中文，不在这里悄悄改它的既有输出（en/zh-HK 靠 detail_i18n 重画）。
    三语 detail 复用 critic 里同一组计数（规范口径），保证数字一致、可复现。"""
    opt, rsk = critic["optimistic_hits"], critic["risk_hits"]
    zh_c = _critic_verdict(opt, rsk, "zh-CN")
    en_c = _critic_verdict(opt, rsk, "en")
    tw_c = _critic_verdict(opt, rsk, "zh-HK")
    details = {
        "zh-CN": f"乐观词 {opt} 处 · 风险词 {rsk} 处 → 判定「{zh_c['flag']}」（{zh_c['note']}）",
        "en": f'Optimistic terms {opt} · risk terms {rsk} → verdict "{en_c["flag"]}" ({en_c["note"]})',
        "zh-HK": f"樂觀詞 {opt} 處 · 風險詞 {rsk} 處 → 判定「{tw_c['flag']}」（{tw_c['note']}）",
    }
    return {
        "kind": "critic",
        "title": _loc(
            lang, "批评 · 美化偏见检测", "Critique · optimism-bias check", "批評 · 美化偏見檢測"
        ),
        "title_i18n": _loc3(
            "批评 · 美化偏见检测", "Critique · optimism-bias check", "批評 · 美化偏見檢測"
        ),
        "detail": details.get(lang, details["zh-CN"]),
        "detail_i18n": _loc3(details["zh-CN"], details["en"], details["zh-HK"]),
    }


def _trace_calib(lang="zh-CN"):
    """校准步骤轨迹（lang 缺省 zh-CN，原因同 _trace_critic）。"""
    zh_d = (
        "本次输出未接入「逐轮向真实客户拉近」的校准流程（达标要求：违约率分布差距 ≤ 0.05），"
        "因此固定标注「未校准」，不得直接用于授信决策。"
    )
    en_d = (
        "This output is not connected to the calibration loop that moves each round toward "
        "real customers (target: default-rate distribution gap ≤ 0.05), so it is permanently "
        'labelled "uncalibrated" and must not be used directly for credit decisions.'
    )
    tw_d = (
        "本次輸出未接入「逐輪向真實客戶拉近」的校準流程（達標要求：違約率分佈差距 ≤ 0.05），"
        "因此固定標註「未校準」，不得直接用於授信決策。"
    )
    return {
        "kind": "calib",
        "title": _loc(lang, "校准 · 状态", "Calibration · status", "校準 · 狀態"),
        "title_i18n": _loc3("校准 · 状态", "Calibration · status", "校準 · 狀態"),
        "detail": _loc(lang, zh_d, en_d, tw_d),
        "detail_i18n": _loc3(zh_d, en_d, tw_d),
    }


# --------------------------------------------------------------------------
# 确定性兜底生成器（无 key / 无依赖 / LLM 失败时）
# --------------------------------------------------------------------------

_PROFILES = [
    ("小李", "深圳", "互联网运营", 9000, 4500, 1),
    ("王姐", "广州", "零售店员", 6500, 5200, 2),
    ("陈先生", "东莞", "制造业技工", 11000, 6000, 0),
    ("赵女士", "佛山", "自由职业", 7000, 6800, 1),
]


def _seeded(hypothesis):
    h = int(hashlib.sha256(hypothesis.encode("utf-8")).hexdigest(), 16)
    return h


def _fallback(hypothesis, rule_desc=None, profile=None, lang="zh-CN", rule=None, decompose=None):
    """确定性兜底叙事：以模拟对象（真实贷款）的事实为骨架，可复现、离线、明确标注 synthetic。
    按 lang 输出（前端切语言时透传；zh-CN 默认）。

    没有模拟对象时退回哈希画像并在轨迹中明示（合并页正常流程总是带 profile）。
    rule 是拟议规则 dict（供 *_i18n 变体按语言各取 rule_desc）；只传 rule_desc 字符串
    的调用（旧签名 / 测试）三语变体共用该串。
    decompose 是已确认的分解冲击条件（可选）：把条件写进兜底结论模板，
    让结论围绕该条件展开（与 _run_agent 的 cond_desc 同口径）。

    resolved 单语输出沿用改动前的组装，逐字节不变；narrative / inference 同时附
    *_i18n 三语变体（_loc3），供前端随当前语言整句重画。铁律：i18n["zh-CN"] 与
    lang="zh-CN" 时的 resolved 逐字节一致（pytest 断言）。"""
    profile = profile if isinstance(profile, dict) and profile.get("loan_id") else None
    t = hypothesis
    cond_desc = _decompose_cond_desc(
        (decompose or {}).get("conditions") if isinstance(decompose, dict) else None, lang
    )

    # 拟议规则描述按语言各取一次（「LTV 上限 / LTV cap」标签随语言；scope/action 标签
    # 是前端透传的数据值，本就随请求语言）。resolved 路径沿用调用方传入的 rule_desc。
    rule_descs = {}
    if rule_desc:
        for lg in _I18N_LANGS:
            rd_lg = _rule_desc(rule, lg) if isinstance(rule, dict) else None
            rule_descs[lg] = rd_lg if rd_lg else rule_desc

    if profile:
        # ---- 模拟对象 = 作用域内真实贷款：叙事用这笔贷款的真实事实 ----
        narrative_zh = (
            f"模拟对象取自规则作用域内真实贷款 #{profile['loan_id']}"
            + (f"（{profile['city_name']}）" if profile.get("city_name") else "")
            + f"：余额 {profile.get('balance', 0):,.0f} 元"
            + (f"、LTV {profile['ltv'] * 100:.2f}%" if profile.get("ltv") is not None else "")
            + (
                f"、月收入约 {profile['income_monthly']:,.0f} 元"
                if profile.get("income_monthly")
                else ""
            )
            + (
                f"、偿债比 {profile['debt_ratio']:.2f}"
                if profile.get("debt_ratio") is not None
                else ""
            )
            + (f"、信用分 {profile['credit_score']}" if profile.get("credit_score") else "")
            + (f"、当前分类「{profile['risk_class']}」" if profile.get("risk_class") else "")
            + "。基于假设推演其可能的偿付压力与行为响应。"
        )
        narrative_en = (
            f"The simulated subject is a real loan #{profile['loan_id']}"
            + (f" ({profile['city_name']})" if profile.get("city_name") else "")
            + f": balance {profile.get('balance', 0):,.0f} yuan"
            + (f", LTV {profile['ltv'] * 100:.2f}%" if profile.get("ltv") is not None else "")
            + (
                f", monthly income ~{profile['income_monthly']:,.0f} yuan"
                if profile.get("income_monthly")
                else ""
            )
            + (
                f", debt-service ratio {profile['debt_ratio']:.2f}"
                if profile.get("debt_ratio") is not None
                else ""
            )
            + (f", credit score {profile['credit_score']}" if profile.get("credit_score") else "")
            + (f', current tier "{profile["risk_class"]}"' if profile.get("risk_class") else "")
            + ". Based on the hypothesis, we infer its likely repayment pressure and behavioral response."
        )
        narrative_tw = (
            f"模擬對象取自規則作用域內真實貸款 #{profile['loan_id']}"
            + (f"（{profile['city_name']}）" if profile.get("city_name") else "")
            + f"：餘額 {profile.get('balance', 0):,.0f} 元"
            + (f"、LTV {profile['ltv'] * 100:.2f}%" if profile.get("ltv") is not None else "")
            + (
                f"、月收入約 {profile['income_monthly']:,.0f} 元"
                if profile.get("income_monthly")
                else ""
            )
            + (
                f"、償債比 {profile['debt_ratio']:.2f}"
                if profile.get("debt_ratio") is not None
                else ""
            )
            + (f"、信用分 {profile['credit_score']}" if profile.get("credit_score") else "")
            + (f"、當前分類「{profile['risk_class']}」" if profile.get("risk_class") else "")
            + "。基於假設推演其可能的償付壓力與行為響應。"
        )
        if any(k in t for k in ("利率下调", "降息", "减息")):
            inf_zh = "利率下调使该笔月供压力边际缓解，提前还款/增贷意愿上升，但收入未同步改善时真实违约率改善有限。"
            inf_en = "A rate cut marginally eases this loan's monthly payment pressure and raises prepayment/additional-loan willingness, but real default-rate improvement is limited when income does not improve in parallel."
            inf_tw = "利率下調使該筆月供壓力邊際緩解，提早還款／增貸意願上升，但收入未同步改善時真實違約率改善有限。"
        elif any(k in t for k in ("利率上调", "加息", "升息")):
            inf_zh = "利率上行直接推高该笔月供，偿债缓冲收窄，违约概率上行。"
            inf_en = "Rising rates directly push up this loan's monthly payment, narrowing the repayment buffer and raising default probability."
            inf_tw = "利率上行直接推高該筆月供，償債緩衝收窄，違約概率上行。"
        elif any(k in t for k in ("房价下跌", "抵押物价跌", "房价下行")):
            inf_zh = (
                "抵押物估值下行推高该笔 LTV，不足值风险暴露，触发强预警线概率上升，贷后应提前介入。"
            )
            inf_en = "A drop in collateral valuation raises this loan's LTV, exposing shortfall risk and increasing the chance of crossing the strong-alert line; post-lending intervention should come early."
            inf_tw = "抵押物估值下行推高該筆 LTV，不足值風險暴露，觸發預警紅線的概率上升，貸後應提前介入。"
        elif any(k in t for k in ("失业", "收入下降", "经济下行")):
            inf_zh = "收入端冲击下该笔偿债缓冲收窄，任何收入中断都可能触发违约链，建议纳入压力测试尾部情景。"
            inf_en = "Under an income-side shock this loan's repayment buffer narrows; any income interruption can trigger a default chain, so it should be included in stress-test tail scenarios."
            inf_tw = "收入端衝擊下該筆償債緩衝收窄，任何收入中斷都可能觸發違約鏈，建議納入壓力測試尾部情境。"
        else:
            inf_zh = "按基线推演：该笔的情景传导以偿付端为主，LTV 门槛对收入端冲击无约束。"
            inf_en = "Baseline inference: this loan's scenario transmission is mainly on the repayment side; the LTV threshold does not constrain income-side shocks."
            inf_tw = "按基線推演：該筆的情境傳導以償付端為主，LTV 門檻對收入端衝擊無約束。"
        gen_detail = _loc(
            lang,
            "未配置真实 LLM，叙事由确定性模板生成，"
            f"以模拟对象事实为骨架（{_profile_desc(profile)}）——不参与定量结论",
            "No real LLM configured; the narrative is generated by a deterministic template, built on the "
            f"simulated subject's facts ({_profile_desc(profile)}) — not part of the quantitative conclusion",
            "未配置真實 LLM，敘事由確定性模板生成，"
            f"以模擬對象事實為骨架（{_profile_desc(profile)}）——不參與定量結論",
        )
    else:
        # ---- 无模拟对象：哈希画像（历史路径，轨迹中明示） ----
        h = _seeded(hypothesis)
        name, city, job, income, expense, kids = _PROFILES[h % len(_PROFILES)]
        narrative_zh = (
            f"{name}（{city}·{job}）月收入约 {income} 元、月支出约 {expense} 元、"
            f"抚养子女 {kids} 人；收支比 {expense / income * 100:.0f}%，"
            f"无显著储蓄缓冲。基于假设推演其可能的行为与风险响应如下。"
        )
        narrative_en = (
            f"{name} ({city}·{job}) has monthly income ~{income} yuan, monthly expense ~{expense} yuan, "
            f"{kids} dependents; income/expense ratio {expense / income * 100:.0f}%, "
            f"with no significant savings buffer. Based on the hypothesis we infer its likely behavior and risk response."
        )
        narrative_tw = (
            f"{name}（{city}·{job}）月收入約 {income} 元、月支出約 {expense} 元、"
            f"撫養子女 {kids} 人；收支比 {expense / income * 100:.0f}%，"
            f"無顯著儲蓄緩衝。基於假設推演其可能的行為與風險響應如下。"
        )
        inf_zh = (
            f"假设未命中预设情景规则，按基线推演：{name} 当前收支比 "
            f"{expense / income * 100:.0f}%，处于{'承压' if expense / income > 0.5 else '稳健'}区间。"
        )
        inf_en = (
            f"The hypothesis does not hit a preset scenario rule; baseline inference: {name}'s current "
            f"income/expense ratio is {expense / income * 100:.0f}%, in the "
            f"{'stressed' if expense / income > 0.5 else 'stable'} range."
        )
        inf_tw = (
            f"假設未命中預設情境規則，按基線推演：{name} 當前收支比 "
            f"{expense / income * 100:.0f}%，處於{'承壓' if expense / income > 0.5 else '穩健'}區間。"
        )
        gen_detail = _loc(
            lang,
            "未配置真实 LLM，叙事由确定性模板生成：合成画像由假设哈希选取、"
            "可复现——仅为行为叙事示例，不参与定量结论",
            "No real LLM configured; the narrative is generated by a deterministic template: the synthetic "
            "profile is chosen by hashing the hypothesis, reproducible — it is only a behavioral narrative "
            "example, not part of the quantitative conclusion",
            "未配置真實 LLM，敘事由確定性模板生成：合成畫像由假設哈希選取、"
            "可複現——僅為行為敘事示例，不參與定量結論",
        )

    narrative = _loc(lang, narrative_zh, narrative_en, narrative_tw)
    inf_map = {"zh-CN": inf_zh, "en": inf_en, "zh-HK": inf_tw}

    # 拟议规则作为评估对象（合并页打通语义）：结论必须回到「这条规则够不够」。
    # 追加段整段按语言重组（subject 与 rule_desc 都随语言）；zh-CN 拼完后与
    # 改动前 lang="zh-CN" 的 resolved 逐字节一致。
    if rule_desc:
        subjects = {
            "zh-CN": "该笔贷款" if profile else "此类客群",
            "en": "this loan" if profile else "this customer segment",
            "zh-HK": "該筆貸款" if profile else "此類客群",
        }
        appends = {
            "zh-CN": (
                f" 对照拟议规则（{rule_descs['zh-CN']}）：该情景下{subjects['zh-CN']}的边际恶化若先表现为"
                "抵押物不足值，规则门槛可拦截存量恶化；但收入端冲击不经过 LTV 门槛，"
                "规则覆盖不到——残余缺口建议回退人工复核。"
            ),
            "en": (
                f" Against the proposed rule ({rule_descs['en']}): if this scenario's marginal deterioration of "
                f"{subjects['en']} first shows as collateral shortfall, the rule threshold can intercept the "
                f"deteriorating stock; but income-side shocks do not pass the LTV threshold, so the rule does "
                f"not cover them — the residual gap should fall back to manual review."
            ),
            "zh-HK": (
                f" 對照擬議規則（{rule_descs['zh-HK']}）：該情境下{subjects['zh-HK']}的邊際惡化若先表現為"
                "抵押物不足值，規則門檻可攔截存量惡化；但收入端衝擊不經過 LTV 門檻，"
                "規則覆蓋不到——殘餘缺口建議回退人工覆核。"
            ),
        }
        for lg in _I18N_LANGS:
            inf_map[lg] += appends[lg]

    # decompose 已确认的冲击条件写进兜底结论：三语各拼一句「围绕该条件推演」。
    if cond_desc:
        cond_appends = {
            "zh-CN": f" 推演围绕已确认的分解冲击条件（{cond_desc}）展开，结论受该条件约束。",
            "en": (
                f" The deduction is built around the confirmed decomposed shock condition "
                f"({cond_desc}); the conclusion is constrained by it."
            ),
            "zh-HK": f" 推演圍繞已確認的分解衝擊條件（{cond_desc}）展開，結論受該條件約束。",
        }
        for lg in _I18N_LANGS:
            inf_map[lg] += cond_appends[lg]

    inference = inf_map.get(lang, inf_map["zh-CN"])
    narrative_i18n = _loc3(narrative_zh, narrative_en, narrative_tw)
    inference_i18n = _loc3(inf_map["zh-CN"], inf_map["en"], inf_map["zh-HK"])

    # 规范口径：乐观/风险词计数以 zh-CN 文本算一次，三语轨迹行复用同一组数字
    # （zh-CN 请求与改动前计数一致；en/zh-HK 请求改用规范口径属有意变更——
    # 保证三语数字一致、可复现）。
    opt, rsk = _critic_counts(narrative_zh + " " + inf_map["zh-CN"])
    critic = _critic_verdict(opt, rsk, lang)

    # 步骤轨迹（叙事侧）：如实交代这段 narrative 怎么来的、可信边界在哪。
    # 情景解析 / 规则评估等定量步骤由 p6_policy._process_chain 提供（真实存量数字），
    # 这里只讲「合成叙事的生成机制」，避免两处口径重复。
    trace = [
        {
            "kind": "gen",
            "title": _loc(
                lang, "叙事生成 · 方式", "Narrative generation · method", "敘事生成 · 方式"
            ),
            "title_i18n": _loc3(
                "叙事生成 · 方式", "Narrative generation · method", "敘事生成 · 方式"
            ),
            "detail": gen_detail,
        },
    ]
    # 推理链前插：decompose.steps 与 _run_agent 同款组装（kind="decompose"，三语齐备）。
    # 兜底路径同样如实呈现推理链（规格 D.2），与下方 cond_desc 写入结论共存不冲突。
    if decompose and isinstance(decompose, dict) and decompose.get("steps"):
        trace = _decompose_trace_pre(decompose["steps"]) + trace
    if profile:
        # profile_desc 双轨：gen_detail（上面 _loc 版）里的骨架整句是中文，en/zh-HK
        # 会泄漏——detail_key + profile_parts/tail 让前端按当前语言重画；detail 保留
        # 为无 key 时的回落原句。zh-CN 渲染结果与 detail 逐字一致。
        # 不给 gen 行加 detail_i18n：骨架翻译走前端 detail_key 机制，两套机制并存
        # 会互相打架（后端组不出 en 整句）。
        _, parts, tail = _profile_desc_tracks(profile)
        trace[0]["detail_key"] = (
            "未配置真实 LLM，叙事由确定性模板生成，"
            "以模拟对象事实为骨架（{profile}）——不参与定量结论"
        )
        trace[0]["profile_parts"] = parts
        trace[0]["profile_tail"] = tail
    if rule_desc:
        trace.append(
            {
                "kind": "rule",
                "title": _loc(lang, "叙事评估对象", "Narrative evaluation subject", "敘事評估對象"),
                "title_i18n": _loc3("叙事评估对象", "Narrative evaluation subject", "敘事評估對象"),
                "detail": _loc(
                    lang,
                    f"拟议规则「{rule_desc}」，叙事结论围绕其覆盖缺口生成",
                    f'Proposed rule "{rule_desc}"; the narrative conclusion is built around '
                    "its coverage gap",
                    f"擬議規則「{rule_desc}」，敘事結論圍繞其覆蓋缺口生成",
                ),
                "detail_i18n": _loc3(
                    f"拟议规则「{rule_descs['zh-CN']}」，叙事结论围绕其覆盖缺口生成",
                    f'Proposed rule "{rule_descs["en"]}"; the narrative conclusion is built around '
                    "its coverage gap",
                    f"擬議規則「{rule_descs['zh-HK']}」，敘事結論圍繞其覆蓋缺口生成",
                ),
            }
        )
    trace.append(_trace_critic(critic, lang))
    trace.append(_trace_calib(lang))
    return {
        "hypothesis": hypothesis,
        "narrative": narrative,
        "inference": inference,
        "narrative_i18n": narrative_i18n,
        "inference_i18n": inference_i18n,
        "synthetic": True,
        "calibration_status": "未校准",
        "critic_flag": critic,
        "model": "deterministic-fallback",
        "provider": "builtin",
        "mode": "deterministic-fallback",
        "trace": trace,
    }
