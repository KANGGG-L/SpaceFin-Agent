# LLM / GenAI 链路台账（LLM Registry）

> **定位**：本文件是 LLM/GenAI 链路的版本台账与守卫常量登记，回答可追溯性契约 Q5（回退方式）与 Q2/Q3（决策与证据）。可追溯性唯一入口是 [`../TRACEABILITY.md`](../TRACEABILITY.md)；相关决策见 [`../adr/ADR-0006-llm-guardrail-policy.md`](../adr/ADR-0006-llm-guardrail-policy.md)。
>
> **兄弟台账**：[AVM 模型卡](AVM_MODEL_CARD.md) · [LLM 回归用例集](LLM_EVAL_CASES.md)
>
> **权威来源**：[`docs/tech/ai-decompose-and-anthropic.md`](../tech/ai-decompose-and-anthropic.md)（功能设计说明）、[`tools/frontend/ai_guard.py`](../../tools/frontend/ai_guard.py)（守卫常量，**常量值以代码为准**）、[`tools/frontend/sandbox_agent.py`](../../tools/frontend/sandbox_agent.py)、[`tools/frontend/ai_decompose.py`](../../tools/frontend/ai_decompose.py)。
>
> **口径提示（诚实）**：设计说明 `ai-decompose-and-anthropic.md` 的守卫常量值（总预算 120s / 读超时 90s / max_tokens 800↔4000）已与代码快照对齐；本台账常量值一律取自 `ai_guard.py`，如再发现文档与代码不一致，以代码为准并回写设计说明。

---

## 应用落点

| 落点 | 文件 | LLM 角色 | 输出形态 |
| --- | --- | --- | --- |
| **P6 NL 规则起草 / 宏推断** | `tools/frontend/pages/p6_policy.py`（`handle_nl_parse` / `handle_decompose`）+ `tools/frontend/ai_decompose.py` + `tools/frontend/ai_guard.py` | 两条链：`nl_parse` = 正则骨架 + LLM 填空（正则值优先，`_merge_nl_draft`）；`decompose` = LLM 主解析（无正则前置）+ 确定性兜底 | JSON 草稿（`scope_type`/`scope_value`/`action`/`ltv_cap`/`shock_text`）；`{steps, conditions, options, rationale, note}` |
| **P10 策略沙盒推演** | `tools/frontend/sandbox_agent.py`（`run_hypothesis` → `_run_agent`） | LangChain `create_agent` + `ChatOpenAI`，模型自主调用风控工具（ReAct 式循环） | JSON `{narrative, inference}`（+ `trace` / `critic_flag` / `calibration_status`） |
| **合成画像生成与校准** | `tools/persona/`（`main.py` / `generator.py` / `critic.py` / `benchmark.py`） | **无 LLM**——确定性 Generator → Critic 校准闭环（固定 seed 自助采样 + 秩分位数映射），用作「LLM 美化偏见」（痛点 P3）的对照基准 | `output/persona/persona_report.json`（KS 报告 + 校准轨迹 + 代表案例） |

> 三处共用的传输/守卫原语在 `ai_guard.py`（0 依赖 stdlib 叶模块）。P6 两条链与 P10 沙盘**共用同一组配置键**（`LLM_KEY`/`LLM_BASE_URL`/`LLM_MODELS`），但 HTTP 客户端不同（P6 直连 urllib；P10 走 langchain）；「硬合并」已决断关闭（won't-do，见 `ai-decompose-and-anthropic.md` §6.1）。
>
> 合成画像与 Critic 校准的指标（`persona_ks_naive` / `persona_ks_calibrated` / `persona_ks_target`）见 TRACEABILITY §2，决策见 [`../adr/ADR-0008-h3-parked-no-ground-truth.md`](../adr/ADR-0008-h3-parked-no-ground-truth.md)。

## 后端与模型

- **协议**：OpenAI 兼容 `POST {base}/chat/completions`（`Authorization: Bearer`，UA `spacefin-nl/1.0`；`ai_guard._nl_llm_request`，`ai_guard.py:280-326`）。`ai_guard._nl_extract_text` 对 Anthropic content 块数组的兼容解析是对历史响应格式的防御性兜底（`ai_guard.py:241-261`）。
- **环境键**（`ai_guard._nl_kv` / `sandbox_agent._llm_config`；`.env.example:82-99`）：
  - `LLM_KEY` —— API Key，必填才能走真实 LLM；缺 key 静默走确定性/正则路径。
  - `LLM_BASE_URL` —— OpenAI 兼容基础地址（代码自动拼 `/chat/completions`）；缺省 `NL_LLM_DEFAULT_BASE`。
  - `LLM_MODELS` —— 模型**回退链**，逗号分隔，首个为主；未配置时退回 `LLM_MODEL` 单值；都没有用默认（`ai_guard._nl_llm_models`，`ai_guard.py:232-238`）。
  - `SPACEFIN_NL_LLM` —— 只关 P6 解析侧的开关（`0`=关，缺省=开）。
- **默认 base / model 常量**（代码，`ai_guard.py:55-56`）：
  - `NL_LLM_DEFAULT_BASE = "https://api.mistral.ai/v1"`
  - `NL_LLM_DEFAULT_MODEL = "mistral-small-2603"`
  - `sandbox_agent._llm_config` 的缺省 base 亦复用 `ai_guard.NL_LLM_DEFAULT_BASE`（`sandbox_agent.py:62`）。
- **已废弃键**：早期版本 `SPACEFIN_LLM_API_KEY` / `SPACEFIN_LLM_MODELS` **已废弃，现无任何代码读取**（`ai-decompose-and-anthropic.md` §2；全仓库检索确认仅该文档提及）。
- **P10 沙盘模型参数**（`sandbox_agent._run_agent:489-497`）：`ChatOpenAI(model=model, temperature=0.3, max_tokens=1500, timeout=NL_LLM_READ_TIMEOUT, max_retries=1)`。
- **`.env.example` 与代码默认的差异（如实标注）**：`.env.example` 的注释示例指向 commandcode 网关（`https://api.commandcode.ai/provider/v1` + `deepseek/deepseek-v4-flash-fast`），而**代码内置缺省是 Mistral**（见上）。实际走哪个后端取决于部署时 `.env` 的 `LLM_BASE_URL`/`LLM_MODELS`；未配置时用代码缺省。

## 护栏常量

以下常量**均逐字取自 `tools/frontend/ai_guard.py`**（代码即权威）：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `NL_LLM_MAX_CHARS` | `2000 if _DEV_RELAX else 200` | 输入字符上限（清洗后截断） |
| `_DEV_RELAX` | `SF_DEV_MODE in ("1","true","True")` | 开发态放宽开关（`dev_server.py` 注入） |
| `NL_LLM_TOTAL_BUDGET` | `120` | 整条模型回退链的总耗时上限（秒，同步 handler） |
| `NL_LLM_READ_TIMEOUT` | `90` | 单次请求读超时预算（秒） |
| `NL_LLM_ATTEMPTS` | `2` | 同一模型对「读超时」的最大尝试次数（其余错误直接换模型） |
| `NL_LLM_RATE_PER_MIN` | `30 if _DEV_RELAX else 3` | 每桶（端点）每用户每分钟调用上限 |
| `NL_LLM_RATE_PER_DAY` | `300 if _DEV_RELAX else 30` | 每桶（端点）每用户每日调用上限 |
| `NL_LLM_CACHE_MAX` | `200` | 进程内结果缓存条数上限 |
| `NL_LLM_CACHE_TTL` | `3600` | 缓存 TTL（秒） |
| `NL_LLM_RESP_MAX_BYTES` | `524288` | 上游响应体字节上限（防超大响应拖垮解析/内存） |
| `NL_LLM_RAW_MAX_CHARS` | `20000` | 抽取文本字符上限（模型输出按此截断后再进校验/展示） |
| `NL_LLM_MAX_TOKENS` | `800` | `_nl_llm_request` 的**默认** `max_tokens`（即 **nl_parse** 调用点的取值，`handle_nl_parse` 不传该形参） |
| `NL_LLM_DECOMPOSE_MAX_TOKENS` | `4000` | **decompose 调用点**传的 `max_tokens`（`ai_decompose.py:1452`） |
| `SAND_AGENT_TOTAL_BUDGET` | `60` | P10 沙盘单次推演总耗时预算（秒，`sandbox_agent.py:39`） |

- **两个调用点的 max_tokens**：`ai_guard.py` 的 `max_tokens` 是调用方可传参数（`ai_guard.py:281`、`:337`）——**nl_parse = 800**（缺省，`p6_policy.handle_nl_parse` 走 `_nl_llm_call` 默认值）、**decompose = 4000**（`NL_LLM_DECOMPOSE_MAX_TOKENS`）。P10 沙盘 = 1500（`ChatOpenAI` 侧）。
  > 历史说明：早期设计说明曾记「nl_parse 300 / decompose 1200」，`ai_guard.py` 行内注释也曾保留「300 / 1000」的旧值；当前代码与设计说明均已统一为 **800 / 4000**（换推理模型后其 reasoning_content 会占满较小上限，故放宽）。
- **注入防护**：`_INJECTION_RES`（`ai_guard.py:90-106`，中英双语 12 条模式）命中即拒绝调用；`_sanitize_nl_text`（`:115-130`）去控制字符/零宽、NFKC 归一、剥除字面 `</user_text>` 定界串（防定界逃逸）、空白归一、截断。
- **限流分池与「检查/登记分离」**：`bucket="nl_parse"` 与 `bucket="decompose"` 各自配额（一次确认点击会串行触发两端点）；`_nl_rate_allow` 只检查，请求**成功返回后**才由 `_nl_rate_record` 登记——超时/失败不烧配额（`ai_guard.py:164-188`）。
- **日志纪律**：`_log_nl` 只留指纹（sha256 前 8 + `len`）与耗时，输入原文不落日志（`ai_guard.py:379-388`）。

## 降级链

**统一原则：任一环失败都退回确定性路径，页面永不 500。** 实现文件与触发条件：

| 触发 | P6 `nl_parse` | P6 `decompose` | P10 沙盘 |
| --- | --- | --- | --- |
| 缺 key / LLM 未启用 | `nl_mode="regex-no-key"`，返回正则骨架 | `_deterministic_decompose`（`nl_mode="regex-no-key"`, `fallback_kind="no-key"`） | `_fallback`（`mode="deterministic-fallback"`） |
| 超配额（限流） | `nl_mode="regex-fallback"`（静默） | `fallback_kind="rate-limited"` → `_deterministic_decompose` | （P10 不复用该限流桶） |
| 疑似注入 / 超长输入 | `nl_mode="regex-injection-guard"`（附三语 note） | 注入→`nl_mode="regex-injection-guard"`；超长/空→确定性兜底 | 注入→`_fallback` + `llm_error="injection-blocked"` |
| 依赖缺失（如 `langchain` / `zhconv` 未装） | （无 langchain 依赖） | `zhconv` 缺失仅停用文字体系红线，主链不受影响 | `_run_agent` 内惰性 import 失败 → `_fallback` |
| 调用失败 / 超时 / 超预算 | `nl_mode="regex-fallback"` | `_deterministic_decompose`（`reason` 明示截断/不可解析/连接失败，禁止沉默降级） | `_fallback` + `llm_error`（限长 200） |
| 输出非法（JSON 解析失败 / 逐字段校验不过） | `_validate_llm_draft` 不合规字段**丢弃**；`llm-ok-noop`（零字段补充） | `_validate_options` 全空 → 确定性兜底 | `_validate_agent_fields` 不过 → `raise` → `_fallback` |
| handler 顶层异常 | — | `handle_decompose` 捕获 → 返回空草稿 `degraded=true` | `run_hypothesis` `try/except` → `_fallback` |

- **实现位置**：P6 守卫原语 + 回退链 `ai_guard._nl_llm_call`（总预算硬截止，`ai_guard.py:337-376`）；decompose 确定性兜底 `ai_decompose._deterministic_decompose`（`ai_decompose.py:1128-1288`）；P10 确定性兜底 `sandbox_agent._fallback`（`sandbox_agent.py:895-1179`）+ `run_hypothesis` 的 `try/except`（`:185-217`）。
- **禁止沉默降级**：任何回退都须对用户明示且留痕（rationale 无效回退模板、模型输出无可确认选项、sanitize 截断——`ai_decompose.py` 三处 note 明文标注，文案 key 须在前端双字典注册）。
- **`_macro_steps` 已清空**：decompose 的固定三段 `steps` 模板已移除，返回 `[]`，可读性由 `options` 卡片与 `rationale` 承担（`ai_decompose.py:66-69`）。

## 变更影响面

改动 LLM 链路的任一部分时，必须重跑对应的回归用例集（[LLM_EVAL_CASES.md](LLM_EVAL_CASES.md)）——LLM 无「精度门」，以结构化用例集替代：

| 改动什么 | 必须重跑什么 |
| --- | --- |
| **Prompt**（`_nl_build_prompt` / `_decompose_build_prompt` / `_run_agent` system） | [LLM_EVAL_CASES.md](LLM_EVAL_CASES.md) 的**结构类 + 枚举白名单类**（字段齐全 / JSON 可解析 / 枚举落在配置池内） |
| **工具集**（`sandbox_agent._build_tools`） | **结构类**（`narrative`/`inference` 逐字段校验）+ 轨迹用例（工具调用 `trace` 行） |
| **模型或回退链**（`LLM_MODELS` / `NL_LLM_DEFAULT_MODEL` / `NL_LLM_DEFAULT_BASE`） | **兜底类**（无 key 时确定性路径仍返回合法结构）+ 全量用例（模型输出漂移面最大） |
| **护栏常量**（`NL_LLM_*` / `SAND_AGENT_TOTAL_BUDGET`） | **注入与越界类**（超长输入、控制字符、注入串）+ 兜底类（超预算/超配额仍降级） |
| **业务配置池**（`tools/risk/config.py` 的 `CITY_MAP` / `INDUSTRY_MAP` / `INDUSTRY_CATEGORIES`；`ACTIONS`；LTV 区间） | **枚举白名单类**（城市 / 行业 / 动作 / LTV 区间必须落在配置池内） |

> 提示：`ACTIONS` 在 `p6_policy.py:116-120`（`reject`/`strict`/`notice`），`ai_decompose._ACTIONS` 为本地声明副本（`ai_decompose.py:25`）；配置池的**单点来源**是 `tools/risk/config.py`。

## 证据

- **决策**：[`../adr/ADR-0006-llm-guardrail-policy.md`](../adr/ADR-0006-llm-guardrail-policy.md)（护栏策略）；相关：[`../adr/ADR-0008-h3-parked-no-ground-truth.md`](../adr/ADR-0008-h3-parked-no-ground-truth.md)（合成画像/Critic，无 ground truth）。
- **证据条目**（`docs/evidence/registry.json`，见 TRACEABILITY §2）：`llm_fallback_rate`（41.67%）、`llm_guard_block_rate`（13.89%）、`llm_eval_case_pass_rate`（95.65%）、`llm_p95_latency_s`（40.71s）——由 `tools/frontend/tests/llm_eval_harness.py` 实测登记，等级为 `recorded`（完整复跑需 `.env` 配 `LLM_KEY`；评测产物 `llm_eval_report.json` 随仓库分发，可核对但不含复跑）。
- **台账一致性（可复算，`verified`）**：本文件的**护栏常量表**与代码数值的一致性由 `python3 tools/dev/gen_ledger.py --check` 强制——文档值与代码赋值表达式的数值字面量集合必须一致，否则 CI 失败（防「改代码没改文档」式漂移）。
- **代码事实（可直接复算，`verified`）**：本文件的**护栏常量**全部逐字取自 `tools/frontend/ai_guard.py` / `sandbox_agent.py`，可由仓库文件直接核对。
- **测试锚点**：`tools/frontend/tests/test_ai_guard.py`（max_tokens 透传 / finish_reason / 限流 / 注入）、`test_ai_decompose.py`、`test_p6_nl_llm.py`、`test_p10_sandbox.py`。
- **口径与限制**：指标由评测 harness 采集，样本为用例集（非生产流量）；单次运行 + LLM 非确定性，详见 [LLM_EVAL_CASES.md](LLM_EVAL_CASES.md) 「当前可观测性」段。
