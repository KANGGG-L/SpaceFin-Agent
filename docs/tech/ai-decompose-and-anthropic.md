# 推演 AI 化 · AI 拆解与 LLM 接入（ai_decompose + P6 NL 解析 + P10 沙盘）

> 本文说明「推演 AI 化」的功能设计与当前实际行为：AI 拆解（decompose）、P6 自然语言解析（nl_parse）、P10 沙盘三处的 LLM 接入方式与共享守卫层，供维护定位。文内代码行号对应编写时的快照，仅用于定位符号。

## 1. 背景与现状总览

P6 策略推演中心有两条 NL 链路 + 一处 P10 沙盘消费点，全部构建在 0 依赖共享守卫层 `tools/frontend/ai_guard.py` 之上：

- **`POST /api/policy/nl_parse`**（`handle_nl_parse`，p6_policy.py:2620）——自然语言 → 拟议规则草稿。现状是「**正则骨架 + LLM 填空（正则值优先）**」：先跑离线 `_regex_parse_nl` 出确定性骨架，再由 LLM 主解析补空缺；正则解析出的值绝对优先，模型只填空缺/切分冲击子句（`_merge_nl_draft`，p6_policy.py:2470）。
- **`POST /api/policy/decompose`**（`handle_decompose`，p6_policy.py:2685）——AI 多步推理：一条粗略想法 → rationale + 1–3 个候选推演条件选项（options）。现状是「**LLM 主解析 + 确定性兜底**」：一律走 LLM 宏观推断（`inferred=1`），**不设正则前置**；LLM 不可用/失败/输出非法时由 `ai_decompose._deterministic_decompose` 确定性兜底，绝不 500（ai_decompose.py:1128-1334）。
- **P10 策略沙盒**（`sandbox_agent`）——真实 LLM 走 langchain `create_agent` + **`ChatOpenAI`**（OpenAI 兼容 `/chat/completions`，sandbox_agent.py:483-494，max_tokens=1500；`create_agent` :569），与 P6 共用同一组配置键。

> 协议说明：P6/P10 现统一走 **OpenAI 兼容** `/chat/completions`；`ai_guard._nl_extract_text` 里对 Anthropic content 块数组的兼容解析，是对历史响应格式的防御性兜底（ai_guard.py:239-247）。

## 2. 配置与键名（与根目录 `.env.example` 对齐）

LLM 配置键**统一**为（ai_guard.py:131-155、sandbox_agent.py:59-64；`.env.example:72-77` 同口径）：

| 键                | 含义                                                                                           | 缺省                                                                          |
| ----------------- | ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `LLM_KEY`         | API Key（必填才能走真实 LLM；缺 key 静默走确定性/正则路径）                                    | 无                                                                            |
| `LLM_BASE_URL`    | OpenAI 兼容基础地址（代码自动拼 `/chat/completions`）                                          | `https://api.mistral.ai/v1`（`ai_guard.NL_LLM_DEFAULT_BASE`，ai_guard.py:55） |
| `LLM_MODELS`      | 模型回退链，逗号分隔（首个为主）；无则退 `LLM_MODEL` 单值                                      | `mistral-small-2603`（`ai_guard.NL_LLM_DEFAULT_MODEL`，ai_guard.py:56）       |
| `SPACEFIN_NL_LLM` | **只关 P6 解析侧**的 LLM（叙事/沙盘不受影响）：`0` = 关                                        | 缺省 = 开（ai_guard.py:148-155）                                              |
| `SF_DEV_MODE`     | 开发态放宽（`dev_server.py:196` 注入）：输入上限 200→2000 字符、限流 3/分·30/日 → 30/分·300/日 | 生产不设即严格默认（ai_guard.py:54-78）                                       |

> 早期版本的 `SPACEFIN_LLM_API_KEY` / `SPACEFIN_LLM_MODELS` 键名**已废弃**，现无任何代码读取。

## 3. 共享守卫层 ai_guard（协议 / 限流 / 注入防护 / max_tokens）

`tools/frontend/ai_guard.py` 是 0 依赖（仅 stdlib）的传输/守卫叶模块，p6_policy、ai_decompose 按名导入，共享同一组限流态与缓存（`_NL_LOCK/_NL_LLM_CALLS/_NL_LLM_CACHE` 进程内单例，ai_guard.py:102-104）。

**协议**：`_nl_llm_request`（ai_guard.py:272-320）走 **OpenAI 兼容协议**——`POST {base}/chat/completions` + `Authorization: Bearer`，UA `spacefin-nl/1.0`；prompt 由调用方经 `build_prompt(text) -> (system, user)` 注入。响应体上限 512KB、抽取文本截断 20000 字符（上游信任边界硬化，ai_guard.py:75-78）；`_extract_json_object` 从回复抠 JSON 对象，失败返回 None 绝不退化为原文（ai_guard.py:204-222）；`finish_reason` 为 length/max_tokens 时 decompose 会给出「模型输出被截断」的明确降级原因（ai_guard.py:256-271、ai_decompose.py:1459-1467）。

**限流分池**：`_nl_rate_allow(user, bucket)` 按**端点分桶**——nl_parse 用 `bucket="nl_parse"`（p6_policy.py:2663）、decompose 用 `bucket="decompose"`（ai_decompose.py:1422），各自 **3 次/分、30 次/日**（SF_DEV_MODE 下 30/分·300/日）。分池原因：一次确认点击会串行触发两个端点，共享池会把单次操作烧掉 2/3 配额（ai_guard.py:156-175）。**检查与登记分离**：`_nl_rate_allow` 只做检查，请求**成功返回后**才由 `_nl_rate_record` 登记（p6_policy.py:2673、ai_decompose.py:1456）——超时/失败的调用不烧配额。

**max_tokens 按端点注入**：请求原语的 `max_tokens` 是调用方可传参数（ai_guard.py:281、337）——nl_parse 不传，走缺省 **800**（`NL_LLM_MAX_TOKENS`，ai_guard.py:63；p6_policy.py:2670），decompose 传 **4000**（`NL_LLM_DECOMPOSE_MAX_TOKENS`，ai_guard.py:71）。原因：decompose 要输出 rationale + 1–3 个 option（各含 reason/explains/impact/三语 text_i18n），中文 JSON 轻松超 300 token；且当前默认推理模型（deepseek-v4-flash-0731）会先烧一部分 token 在 reasoning_content 上，300/1200 都会让 content 为空 → 静默兜底；4000 可完整输出（实测 ~50s）。

**注入防护与定界处理**：

- `_sanitize_nl_text`（ai_guard.py:107-124）：去控制字符/零宽、NFKC、空白归一、截断；并**剥除字面 `</user_text>` 定界串**（大小写/空白变体一并覆盖）——用户文本嵌在 `<user_text>…</user_text>` 内送模型（p6 `_nl_build_prompt` 与 decompose `_decompose_build_prompt` 同款，ai_decompose.py:1553），原话带定界串可提前闭合定界块、伪造模型可执行上下文。
- `_looks_like_injection` + `_INJECTION_RES`（ai_guard.py:82-99）：中英双语注入模式，命中即拒绝调用（宁可误伤回退，也不把可能是指令的内容送进不可信模型）。URL 类模式已收窄：只拦指向本机/内网/云元数据端点的地址——裸 URL 的合法业务引用（「参考 https://… 的数据」）不再被整条误判为注入（历史 bug，ai_guard.py:92-97）。
- 疑似注入 / 超长输入均不调模型：nl_parse 返回 `nl_mode=regex-injection-guard` / `regex-fallback` 并附三语 note；decompose 直接走确定性兜底。

## 4. 两条 NL 链的行为矩阵

|                 | nl_parse（拟议规则草稿）                                                                                           | decompose（推演条件选项）                                                                                                                                                                                    |
| --------------- | ------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 解析路径        | 正则骨架 `_regex_parse_nl` 先行；LLM 主解析，**正则值优先**、模型只填空缺（`_merge_nl_draft`）                     | **LLM 主解析，无正则前置**；可算直陈类输入同样走 LLM                                                                                                                                                         |
| LLM 失败/不可用 | 静默回正则骨架（`regex-fallback`/`regex-no-key`）                                                                  | `_deterministic_decompose` 确定性兜底（规则表 `_DET_RULES` + 纯规则建议分支），`degraded=true`                                                                                                               |
| 输出校验        | `_validate_llm_draft` 逐字段白名单（zone 永不接受；scope 城市码/行业 slug 池；ltv 区间；shock_text 复验+原句子串） | `_validate_options`/`_validate_option` 逐字段白名单 + `rationale`/`reason`/`explains` 校验；**冲击红线**：原话没提冲击（`_has_shock_signal`）时模型编的冲击一律剥成纯规则建议（`_strip_shock_to_rule_only`） |
| 限流桶          | `bucket="nl_parse"`                                                                                                | `bucket="decompose"`                                                                                                                                                                                         |
| max_tokens      | 800（`NL_LLM_MAX_TOKENS` 缺省）                                                                                    | 4000（`NL_LLM_DECOMPOSE_MAX_TOKENS`）                                                                                                                                                                        |
| 缓存 key        | `clean`（清洗后原句）                                                                                              | `"decompose:" + lang + ":" + clean`（语言入 key，不同语言的输出不互串）                                                                                                                                      |
| 只读纪律        | 不写审计、不落库                                                                                                   | 同左；另**软 admin 闸门**：非 admin 得只读草稿——`inferred` 归 0、推断文案收敛为受限说明、`nl_mode="restricted"`（前端不展示「确认」入口，p6_policy.py:2747-2777）                                            |
| 日志            | `_log_nl` 指纹（sha256 前 8 + len），原文不落日志（p6_policy.py:2657、2680）                                       | 原始想法不落日志——handler 不自行记原文（p6_policy.py:2700-2701），指纹工具在 ai_guard.py:371-381                                                                                                             |

## 5. S2 两阶段确认门与 shock 复验闭环

**S2 两阶段确认门**（前端 `p6_policy.js`，守门测试 `test_p6_s2_confirm_gate.py`）：有 NL 输入时，「运行推演」**首点只做解析 + 复述**（`parseAndFillRule` 固化 `st.nlDraft`、渲染推断说明卡、短路返回绝不运行），用户核对无误后**第二次点击**才走草稿真运行（runSimulation 顶部路由，p6_policy.js:3897-3933）；手改输入/表单即作废草稿（`invalidateDraft`）重新裁决闸门。非 NL 路径（回放 / P5 预填 / 载入已生效规则）不被确认环节误伤，直接可运行。

**shock 复验闭环**（冲击只能来自原话，不能来自模型想象力）：

- nl_parse 侧：`_shock_verified`（p6_policy.py:2408-2429）要求冲击子句能被现有冲击解析器（`parse_scenario` + 房价/利率/收入正则）复验；`_validate_llm_draft` 还要求 shock_text 清洗后是**原句的子串**（p6_policy.py:2432-2469）。
- decompose 侧：`_has_shock_signal` 双通道判定原话是否真的提了冲击（`_DET_RULES` 词表 + `parse_scenario`）；模型不听话时 `_strip_shock_to_rule_only` 把含冲击的选项剥成纯规则建议；幅度经 `_snap_pct_magnitude` 按「距冲击锚点最近的百分比」确定性校正（ai_decompose.py:634 `_snap_pct_magnitude`、:722 `_has_shock_signal`、:742 `_strip_shock_to_rule_only`）。

## 6. 诚实标注的现状与偏差

1. **P6 与 P10 是两条独立的 LLM 调用链**：P6（nl_parse/decompose）走 `ai_guard._nl_llm_request`（urllib 直连 OpenAI 兼容端点 + 注入防护 + 限流缓存，零 langchain 依赖）；P10 沙盘走 langchain `create_agent` + `ChatOpenAI` 的工具循环。二者**共用同一组配置键**（`LLM_KEY`/`LLM_BASE_URL`/`LLM_MODELS`）但 HTTP 客户端不同。「硬合并」已决断关闭（won't-do）：两条链对应两种能力，合并会让 P6 的零依赖守卫路径回归 langchain 依赖。
2. **真模型依赖外部 key 与端点可用性**：key 失效/站点变化时，P6 两侧各自走确定性/正则兜底（`degraded`/`regex-fallback`），功能不崩但 AI 推理不出现。
3. **上游信任边界**：上游输出一律视为不可信数据——逐字段白名单校验（模型自由文本进不了结构化字段）、响应体/文本长度上限、错误只回传 `type(exc).__name__`（不给前端回传 URL/主机名/模型内容，p6_policy.py:2741-2744）、日志只留指纹。第三方端点必然可见 `LLM_KEY` 与完整 prompt，缓解靠 key 轮换/换私有端点。
4. **总预算与超时**：整条模型回退链总预算 `NL_LLM_TOTAL_BUDGET=120s`，单次读超时 `NL_LLM_READ_TIMEOUT=90s`，同一模型对读超时补 1 次重试（`NL_LLM_ATTEMPTS=2`，其余错误直接换模型）——2026-10-14 换用 某推理服务端点 的 deepseek-v4-flash-0731（推理模型，思考不可关）后 decompose 单次实测 ~50s，原 70s/60s 余量不足，故放宽到 120s/90s（ai_guard.py:57-78）。同步 handler 里超预算即放弃，走兜底。
5. **decompose 的 `steps` 已清空**：固定三段模板（情景归类/传导路径/条件建议）已按需求移除（`_macro_steps` 返回 `[]`），可读性由 options 卡片与 rationale 承担（ai_decompose.py:66-67）。rationale 口径：LLM 路径 prompt 要求按「字段直判依据 / 冲击与幅度来源 / 传导链条」三层展开 ≤~200 字，超长**截断不拒收**（`_truncate_at_sentence`，中文上限 200 / 拉丁 700）；确定性兜底的 rationale 由 `_rationale_det_obj` 按命中规则上下文生成（传导链 + 参考幅度 + 备选传导，三语齐备），不再是单句罐头文案。
6. **禁止沉默降级**（ai_decompose.py）：任何回退都必须对用户明示且留痕，不得冒充正常输出——① LLM 成功但 rationale 无效 → 回退模板时在 `notes` 追加「模型未返回有效推导说明，已回退模板文案」（三语 note，zh 原句即 key，前端双字典注册）并记 `_NL_LOG.warning`；② 模型输出无可确认选项 → 兜底的 `fallback_reason_key` 明说「模型输出未产生可确认的推演选项」，不冒充「无法连接服务」；③ sanitize 截断 → reason 明说「原话过长被截断，已按确定性规则解析」。文案 key 须在 p6_policy.js en/zh-HK 双字典注册（test_silent_degradation_keys_registered_in_both_dictionaries 钉住）。

## 7. 相关文件

- 守卫/推理服务：`tools/frontend/ai_guard.py`（传输/守卫原语）、`tools/frontend/ai_decompose.py`（decompose 主逻辑 + 确定性兜底）
- P6 接线：`tools/frontend/pages/p6_policy.py`（`handle_nl_parse` / `handle_decompose` / `_validate_llm_draft` / `_merge_nl_draft` / `_shock_verified`）
- P10 沙盘：`tools/frontend/sandbox_agent.py`（`_llm_config` 统一键 + `create_agent`/`ChatOpenAI`）、`tools/frontend/pages/p10_sandbox.py`
- 前端：`tools/frontend/static/pages/p6_policy.js`（S2 两阶段闸门 + decompose 渲染区）
- 测试：`tools/frontend/tests/test_ai_decompose.py`、`test_p6_s2_confirm_gate.py`、`test_p6_nl_llm.py`、`test_p10_sandbox.py` 等

## 语言红线与缓存纪律

- **缓存必须在全部校验闸门之后**：decompose 的 LLM 原始输出先过选项校验 / 冲击红线剥离 / 语言红线（`_free_text_is_cjk_dominant`），全过才写 `_nl_cache_put`。被红线拒绝的输出一旦入缓存，同文请求会毫秒级直达降级，且模型行为改善也被旧缓存钉死。
- **模型输出语言是间歇性漂移的**：模型对英文短输入会以一定概率无视中文语言声明回中文（deepseek 约 1/3）。缓解三层：prompt 对非 zh-CN 请求附加英文硬性约束（写明违规后果）；语言红线触发时 en/zh-HK 软重试一次（跳过缓存读，必走新调用）；最终底线仍是确定性三语兜底。
- **文字体系红线三语覆盖**：`_free_text_is_cjk_dominant` 覆盖三语——en 查字段 CJK 占比 >30%；zh-HK 查简体渗漏、zh-CN 查繁体渗漏（zhconv 整表转换比对，错配占比 >15% 判违规；手写字对表会漏字形共享字，不可用）。逐字段判定而非合并均值，否则英文长文本会把中文 explains 稀释漏网。语言红线触发时任意语言软重试一次。新依赖 `zhconv`（纯 Python，requirements-sandbox.txt，缺失时红线自动降级停用）。
- **模型来源披露**：decompose 成功响应携带 `meta: {model, elapsed_ms, cached}`（`ai_guard._nl_llm_request` 成功路径写回），前端在「AI 多步推理」标题旁展示「由 {model} 生成 · {n}s /（缓存复用）」，降级态槽位让给降级横幅（互斥渲染）。守门：`test_p6_decompose_meta_static.py`。
