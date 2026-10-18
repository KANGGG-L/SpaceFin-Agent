# ADR-0006：LLM 接入采用「0 依赖守卫层 + 确定性兜底」护栏策略

## 状态
已采纳

## 迭代
第 6 轮迭代

## 背景（Context）
- P6 自然语言解析、AI 拆解与 P10 策略沙盒三处接入 LLM；上游模型不可信、外部 key 可能缺失。
- 金融场景要求任何回退都对用户**明示**、不得沉默降级，且页面永不 500。
- 需要一个与业务解耦、可被多条链复用的共享守卫层。
- **观察数据**：接入时链路**无埋点**，兜底 / 拦截 / 通过率不可量化（`llm_fallback_rate` 等登记为 `unavailable`）——无法判断「上游不可信 / 无 key」的实际影响。

## 决策（Decision）
- `tools/frontend/ai_guard.py` 作为 **0 依赖（仅 stdlib）的出入口守卫叶模块**：输入清洗、prompt 注入检测、按端点分池限流、结果缓存、响应体 / 文本长度上限、整链总预算。
- **逐字段白名单校验下沉到调用方**（p6_policy / ai_decompose）：守卫层不 import 业务模块，避免循环 / 耦合。
- `tools/frontend/sandbox_agent.py`：在 `try/except` 内才 import langchain；无 key / 无依赖 / 调用失败一律走确定性兜底生成器，并设总耗时预算。

## 备选方案（Alternatives）
- 在守卫层内做业务白名单：否决——会引入循环 / 耦合依赖，违反 0 依赖纪律。
- 依赖 langchain 统一两条链：否决（won't-do）——会让 P6 的零依赖守卫路径回归 langchain 依赖。
- 无守卫直连模型：否决——注入、限流、超大响应与沉默降级均无防护。

## 影响（Consequences）
**正面**
- 无 key / 上游故障时页面不崩、结果可复现；注入与超长输入被拒。
- 守卫层与业务解耦，可被多条链复用。
- **改变后数据**：经 `tools/frontend/tests/llm_eval_harness.py` 实测（评测集口径）——兜底率 **41.67%**（`llm_fallback_rate`；其中仅 1 次为真实模型回退，发起调用的 22 次里 ≈4.5%）、护栏拦截率 **13.89%**（`llm_guard_block_rate`）、用例通过率 **95.65%**（`llm_eval_case_pass_rate`，22/23）、P95 时延 **40.71s**（`llm_p95_latency_s`）。回归用例集见 `docs/models/LLM_EVAL_CASES.md`。

**代价**
- P6（urllib 直连）与 P10（langchain agent）是两条独立客户端，仅共用配置键，存在理解成本。
- 真实 AI 推理依赖外部 key 与端点可用性；指标由评测 harness 采集，LLM 非确定性（同用例复跑 decompose 可 llm↔regex-fallback）。

## 证据（Evidence）
- `tools/frontend/ai_guard.py` :: `共享叶模块：自然语言 → 结构化的「传输/守卫原语」层（0 依赖，只用 stdlib）`（0 依赖守卫层说明）、`tools/frontend/ai_guard.py` :: `本文件禁止 import components / pages / db / config / sandbox_agent`（白名单下沉调用方）、`tools/frontend/ai_guard.py` :: `NL_LLM_MAX_CHARS = 2000 if _DEV_RELAX else 200`（上限 / 预算 / 限流常量）、`tools/frontend/ai_guard.py` :: `_INJECTION_RES = (`（注入模式）、`tools/frontend/ai_guard.py` :: `def _sanitize_nl_text(text):`、`tools/frontend/ai_guard.py` :: `def _nl_rate_allow(user, bucket="nl"):`（分池）。
- `tools/frontend/sandbox_agent.py` :: `**健壮性**：无 key 或 langchain 未安装`（无 key / 依赖 / 失败降级）、`tools/frontend/sandbox_agent.py` :: `from langchain.agents import create_agent`（调用时才 import langchain）、`tools/frontend/sandbox_agent.py` :: `result = fut.result(timeout=SAND_AGENT_TOTAL_BUDGET)`（总预算硬截止）、`tools/frontend/sandbox_agent.py` :: `def _fallback(hypothesis, rule_desc=None`（确定性兜底）。
- `docs/tech/ai-decompose-and-anthropic.md` :: `## 3. 共享守卫层 ai_guard（协议 / 限流 / 注入防护 / max_tokens）`（守卫层、协议、注入防护、限流分池）、`docs/tech/ai-decompose-and-anthropic.md` :: `**P6 与 P10 是两条独立的 LLM 调用链**`（两条链偏差、总预算、禁止沉默降级）。

## 取证来源（Provenance）
`documented`——护栏实现可在代码直接核对，设计说明见 `docs/tech/ai-decompose-and-anthropic.md`。

## 回退（Rollback）
- 关闭或更换模型链：改 `.env` 的 `LLM_KEY` / `LLM_BASE_URL` / `LLM_MODELS`，或改 `tools/frontend/ai_guard.py` 的护栏常量（输入上限 / 限流 / 超时）。
- 缺 `LLM_KEY` 即走确定性兜底——`ai_guard._nl_llm_enabled` 返回 False，页面不崩并明示降级，无需改代码即可回退到纯正则 / 规则路径。

## 关联（Related）
- [ADR-0008-h3-parked-no-ground-truth.md](ADR-0008-h3-parked-no-ground-truth.md)
- [docs/tech/ai-decompose-and-anthropic.md](../tech/ai-decompose-and-anthropic.md)
- [docs/tech/components/frontend.md](../tech/components/frontend.md)
- [tools/frontend/README.md](../../tools/frontend/README.md)
