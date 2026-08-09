#!/usr/bin/env python
"""策略沙盒 · LangChain 假设推演（精简版新增 AI 能力）。

把用户的一条「假设」（如"某社区消费贷利率下调 1%"）交给 LLM，
生成一段**合成借款人叙事 + 推演结论**，并用轻量 Critic 检测"美化偏见"，
强制打上 `synthetic / 未校准` 标签——延续原沙盒的诚实护栏。

LLM 后端 = 硅基流动（SiliconFlow，OpenAI 兼容）：
    SPACEFIN_LLM_BASE_URL   默认 https://api.siliconflow.cn/v1
    SPACEFIN_LLM_API_KEY    用户提供（必填才能走真实 LLM）
    SPACEFIN_LLM_MODEL      如 Qwen/Qwen2.5-72B-Instruct-128K

**健壮性**：无 key 或 langchain 未安装，或真实 LLM 调用失败 → 一律降级为
确定性兜底生成器，保证离线可跑、页面永不 500。这本文件就是「AI PM 用
LangChain 把假设变成推演叙事」的最小可讲单元，注释即讲解。
"""

import hashlib
import json
import os
import re

# 仓库根：本文件在 <root>/tools/frontend/ 下，回退三层。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
    key = env.get("SPACEFIN_LLM_API_KEY", "").strip()
    base = env.get("SPACEFIN_LLM_BASE_URL", "https://api.siliconflow.cn/v1").strip()
    model = env.get("SPACEFIN_LLM_MODEL", "Qwen/Qwen2.5-72B-Instruct-128K").strip()
    return key, base, model


def run_hypothesis(hypothesis):
    """策略沙盒入口：假设 → 推演结果 dict。

    返回结构（前端直接渲染）：
        hypothesis / narrative / inference / synthetic / calibration_status /
        critic_flag / model / provider / mode / note / llm_error(可选)
    """
    hypothesis = (hypothesis or "").strip()
    if not hypothesis:
        raise ValueError("hypothesis 不能为空")

    key, base, model = _llm_config(_load_env())

    if key:
        try:
            return _run_langchain(hypothesis, key, base, model)
        except Exception as exc:  # 真实 LLM 失败不崩页面：降级兜底并标注
            fb = _fallback(hypothesis)
            fb["llm_error"] = str(exc)
            fb["note"] = "真实 LLM 调用失败，已降级为确定性兜底。"
            return fb

    fb = _fallback(hypothesis)
    fb["note"] = "未配置 SPACEFIN_LLM_API_KEY，使用确定性兜底生成器（演示用，非真实 LLM）。"
    return fb


# --------------------------------------------------------------------------
# 真实 LLM 路径（LangChain）
# --------------------------------------------------------------------------


def _run_langchain(hypothesis, key, base, model):
    # 仅在真正调用时才 import langchain，避免无依赖时整页 500。
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=model,
        api_key=key,
        base_url=base,
        temperature=0.3,
        max_tokens=1500,  # 推理模型（如 DeepSeek-R1）需更大上限容纳思考+输出
    )

    # ChatPromptTemplate：把"假设"安全地填入提示，避免拼接注入。
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "你是房产金融风控的策略推演助手。基于给定假设，生成一段合成借款人叙事与"
                "推演结论。必须保持客观，不得美化借款人风险（不得出现'努力''积极''希望'等"
                "带有社期望偏见的措辞）。只输出一个 JSON 对象，字段："
                "narrative=合成借款人叙事（中文，2-3 句），"
                "inference=推演结论（中文，说明该假设对违约/行为的可能影响，1-2 句）。",
            ),
            ("human", "假设：{hypothesis}\n请只输出 JSON。"),
        ]
    )

    chain = prompt | llm | StrOutputParser()
    raw = chain.invoke({"hypothesis": hypothesis})

    parsed = _extract_json(raw)
    narrative = parsed.get("narrative") or raw
    inference = parsed.get("inference") or ""

    critic = _critic(narrative + " " + inference)
    return {
        "hypothesis": hypothesis,
        "narrative": narrative,
        "inference": inference,
        "synthetic": True,
        "calibration_status": "未校准",
        "critic_flag": critic,
        "model": model,
        "provider": "siliconflow",
        "mode": "langchain-llm",
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

_OPTIMISTIC = ["乐观", "积极", "努力", "希望", "奋斗", "向好", "稳健", "前景广阔", "充满信心"]
_RISK = ["违约", "逾期", "断供", "风险", "下行", "压力", "失业", "资不抵债", "恶化", "降级"]


def _critic(text):
    t = text or ""
    opt = sum(t.count(w) for w in _OPTIMISTIC)
    rsk = sum(t.count(w) for w in _RISK)
    flag = "疑似美化偏见" if (opt >= 2 and opt > rsk) else "未见明显美化偏见"
    return {
        "optimistic_hits": opt,
        "risk_hits": rsk,
        "flag": flag,
        "note": "Critic 仅做关键词启发式检测；真实基准对照属商用前置（H3）。",
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


def _fallback(hypothesis):
    """基于假设哈希的确定性合成借款人（可复现、离线、明确标注 synthetic）。"""
    h = _seeded(hypothesis)
    name, city, job, income, expense, kids = _PROFILES[h % len(_PROFILES)]

    # 假设关键词 → 推演结论（演示用规则，非真实模型）
    t = hypothesis
    if any(k in t for k in ("利率下调", "降息", "减息")):
        inference = (
            f"利率下调使月供压力边际缓解，{name} 类客群提前还款/增贷意愿上升，"
            "但若收入未同步改善，真实违约率改善有限——需警惕'再融资幻觉'。"
        )
    elif any(k in t for k in ("利率上调", "加息", "升息")):
        inference = (
            f"利率上行直接推高 {name} 月供，可支配收入缓冲收窄，"
            "边缘客群（支出/收入比已偏高）违约概率上行，建议收紧审批。"
        )
    elif any(k in t for k in ("房价下跌", "抵押物价跌", "房价下行")):
        inference = (
            f"抵押物估值下行推高 LTV，{name} 抵押物不足值风险暴露，"
            "触发 LTV 强预警线概率上升，贷后保全应提前介入。"
        )
    elif any(k in t for k in ("失业", "收入下降", "经济下行")):
        inference = (
            f"收入端冲击下，{name} 类客群无储蓄缓冲，任何工作中断即触发违约链，"
            "建议压力测试尾部情景。"
        )
    else:
        inference = (
            f"假设未命中预设情景规则，按基线推演：{name} 当前收支比 "
            f"{expense / income * 100:.0f}%，处于{'承压' if expense / income > 0.5 else '稳健'}区间。"
        )

    narrative = (
        f"{name}（{city}·{job}）月收入约 {income} 元、月支出约 {expense} 元、"
        f"抚养子女 {kids} 人；收支比 {expense / income * 100:.0f}%，"
        f"无显著储蓄缓冲。基于假设推演其可能的行为与风险响应如下。"
    )

    critic = _critic(narrative + " " + inference)
    return {
        "hypothesis": hypothesis,
        "narrative": narrative,
        "inference": inference,
        "synthetic": True,
        "calibration_status": "未校准",
        "critic_flag": critic,
        "model": "deterministic-fallback",
        "provider": "builtin",
        "mode": "deterministic-fallback",
    }
