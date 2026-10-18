#!/usr/bin/env python
"""P10 · 策略沙盒推演（设计评审 P10 / R-OPT-01-02，仅出框架）。

设计评审明确「本期仅出交互框架 + 未校准标记规则，不进入本期开发排期」——
本页**不实现**生成→批评→校准的闭环交互，只做三件事：

1. **说明框架**：把「生成（Generator）→ 批评（Critic）→ 校准（Calibration）」
   闭环的三步语义、入口与未来交互形态画在页面上，让评审看得见闭环长什么样；
2. **未校准标记（R-OPT-01）**：读取 `output/persona/persona_report.json`，
   若其中存在 naive 模式（calibration_status="未校准"），页面顶部**醒目标记**
   「未校准输出不得直接用于决策」——这是本页的核心验收点 D-09 / R-OPT-01；
3. **校准证据展示**：naive vs calibrated 的 KS 指标、校准轨迹（trajectory）、
   违约率分布对比，让「未校准 vs 校准」的差距有数据可看。

数据来源 `output/persona/persona_report.json`（tools/persona 产物，output/ 下
.gitignore），报告缺失是正常路径——页面显示占位说明与重建命令，绝不 500。

RBAC：admin / risk / da 可见（策略研究员 / AI PM 视角；贷后不接触策略推演）。
"""

import html
import json
import os
import sys

# pages 以包形式被导入时 frontend 根目录不一定在 sys.path 上（取决于启动方式），
# 这里补一次，保证 `import db` 在 app.py 直跑与 `python -c "import pages"` 两种场景都成立。
_FRONTEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _FRONTEND_DIR not in sys.path:
    sys.path.insert(0, _FRONTEND_DIR)

# 仓库根：本文件在 <root>/tools/frontend/pages/ 下，回退四层。
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
# 策略画像推演报告：tools/persona 的产物。
_PERSONA_FILE = os.path.join(_REPO_ROOT, "output", "persona", "persona_report.json")
# tools/persona 管线目录：用于按需确定性重算逐客户明细（不依赖 report 的 example_cases）。
# 只在这里记下目录，sys.path 的挂载放到 _load_persona_pipeline() 内按需做：
# 模块导入期 insert(0) 会让 tools/persona 下的 main.py / generator.py 等遮蔽同名模块，
# 影响整个进程，而这条路径只有 evidence 接口真正被访问时才需要。
_PERSONA_MOD_DIR = os.path.join(_REPO_ROOT, "tools", "persona")

# 精简版新增：LangChain Agent + 工具调用 假设推演（与 db.py 同目录，已加入 sys.path）。
import sandbox_agent  # noqa: E402


def _load_json(path):
    """读一个 JSON 产物；文件缺失/损坏返回 None（页面降级展示，不 500）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _calib_card(raw):
    """把 naive/calibrated 的 KS 报告压成前端卡片所需的平面结构。

    raw 形如 {"calibration_status": "未校准", "ks_report": {"features": {...},
    "default_prob": {...}}}。报告缺失字段时返回 None，前端优雅降级。
    """
    if not raw:
        return None
    ks = raw.get("ks_report") or {}
    dp = ks.get("default_prob") or {}
    return {
        "bias": raw.get("bias"),
        "calibration_status": raw.get("calibration_status"),
        "ks_features": ks.get("features") or {},
        "ks_default_prob": dp.get("ks"),
        "default_prob_passed": dp.get("passed_ks_le_0_05"),
    }


def _require_admin(ctx):
    """合并进「策略推演中心」（p6_policy）后的角色闸门。

    合并页的页面级 roles 是并集 {admin, risk, da}（规则编辑区三角色都要用），
    而情景推演按 PRD 语义仅 admin 可见 → 这里在 handler 内二次校验，
    与 p6_policy 的 WRITE_ROLES 同一套防御思路（页面级 roles 管不了单区块）。
    """
    return (ctx.user or {}).get("role") == "admin"


def get_sandbox(ctx):
    """读 persona_report.json，组装闭环框架 + 校准状态 + 未校准标记。

    非 admin 返回 available=False + allowed=False（前端据此隐藏沙盘区块，
    不让页面渲染失败）；不裸回 403，避免整页错误态。
    """
    if not _require_admin(ctx):
        return {
            "available": False,
            "allowed": False,
            "message": "情景推演仅对管理员开放。",
        }
    report = _load_json(_PERSONA_FILE)
    rel_path = os.path.relpath(_PERSONA_FILE, _REPO_ROOT)
    if report is None:
        return {
            "available": False,
            # allowed 字段是合并页前端的显式契约：True → 展示沙盘区块。
            "allowed": True,
            # error_key 双轨：前端优先 t(error_key, error_params)，miss 时回落 message 原文。
            # 整句写一行（不拆隐式拼接）：i18n 字典 key 须与原文逐字一致才能命中。
            "message": "策略画像推演报告尚未生成，暂时无法展示校准结果。请联系数据团队运行画像推演分析后刷新本页。当前页面仅展示生成→批评→校准的框架与 R-OPT-01 未校准标记规则。",
            "error_key": "策略画像推演报告尚未生成，暂时无法展示校准结果。请联系数据团队运行画像推演分析后刷新本页。当前页面仅展示生成→批评→校准的框架与 R-OPT-01 未校准标记规则。",
            "error_params": {},
        }

    naive = _calib_card(report.get("naive"))
    calibrated = _calib_card(report.get("calibrated"))
    calibration = report.get("calibration") or {}

    return {
        "available": True,
        "allowed": True,
        "path": rel_path,
        "generated_at": report.get("generated_at"),
        "tool": report.get("tool"),
        "h3": report.get("h3"),
        "acceptance": report.get("acceptance"),
        "params": report.get("params") or {},
        "naive": naive,
        "calibrated": calibrated,
        "calibration": {
            "mechanism": calibration.get("mechanism"),
            "target_ks": calibration.get("target_ks"),
            "max_rounds": calibration.get("max_rounds"),
            "rounds": calibration.get("rounds"),
            "converged": calibration.get("converged"),
            "trajectory": calibration.get("trajectory") or [],
        },
        "comparison": report.get("comparison_default_prob") or {},
        # R2-12：校准证据区「具体案例 · 修正前 vs 修正后」——报告缺该字段时恒为 []
        # （向后兼容：老报告无 example_cases → 前端降级为空、不报错），只增键不破坏现有载荷。
        "example_cases": report.get("example_cases") or [],
        "uncalibrated_rule": report.get("uncalibrated_flag") or {},
        "honest_declaration": report.get("honest_declaration") or {},
        # R-OPT-01：只要报告里存在 naive（未校准）输出，就必须醒目标记。
        "uncalibrated_present": bool(naive) and naive.get("calibration_status") == "未校准",
        # A-5：把「报告含未校准对照」与「修正后是否已达标」拆成两个独立信号——
        # uncalibrated_present 几乎恒真（naive 是修正前基准），不能当"未达标"用；
        # 前端据此用 calibration_converged 决定用红色警告还是中性说明，避免自相矛盾。
        "calibration_converged": calibration.get("converged") is True,
    }


def run_hypothesis(ctx):
    """POST /api/sandbox/hypothesis：假设推演（LangChain）。

    ctx.body 形如 {"hypothesis": "...", "rule": {...}可选}；空假设由
    sandbox_agent 抛 ValueError → 400。rule 是合并页 B 区的拟议规则
    （评估对象），透传后推演结论围绕「该情景下这条规则够不够」生成。
    推演是主动触发的探索动作（区别于 GET 的页面渲染），非 admin 直接 403。
    """
    if not _require_admin(ctx):
        return 403, {"error": "无权限：情景推演仅限管理员使用"}
    body = ctx.body or {}
    rule = body.get("rule")
    profile = body.get("profile")  # preview 选出的模拟对象（作用域内真实贷款）
    lang = body.get("lang") or "zh-CN"  # 前端当前语言，透传给 LLM 决定叙事生成语言
    decompose = body.get("decompose")  # P6「运行推演」第二段确认后的 AI 多步推理产物
    return sandbox_agent.run_hypothesis(
        body.get("hypothesis"),
        rule if isinstance(rule, dict) else None,
        profile if isinstance(profile, dict) else None,
        lang,
        decompose if isinstance(decompose, dict) else None,
    )


# 本模块已合并进「策略推演中心」（pages/p6_policy.py 的 PAGE 注册了 /api/sandbox
# 路由并 import 本文件的 get_sandbox / run_hypothesis）。不再独立注册 PAGE：
# 独立注册会在导航里多出「假设推演」入口，与合并后的单页叙事冲突。
# handler 与校准逻辑保留在本文件，测试（tests/test_p10_sandbox.py）继续直测本模块。


# ---------------- 逐客户 修正前 vs 修正后 明细（iframe 数据源） ----------------
# 设计动机：R2-12 的「具体案例」只抽 3~5 条代表，无法让用户直观看到 1000 个模拟客户
# 在修正前后的逐笔位移。本节提供两条只读接口：
#   1. GET /api/sandbox/evidence_rows : JSON 明细，用于程序化消费
#   2. GET /api/sandbox/evidence_page : 独立 HTML 页，适合前端 iframe 嵌入
# 两者脱敏口径：仅 index（非 customer_id/loan_id）、三特征、两违约概率、delta，
# 外加行业与城市两个属性维度（行业为门类名、城市脱敏到市级）——判读规则影响
# 必须知道每笔画像落在什么作用域（行业/城市/网格），只有三特征对照不出来。
# 永远不返回 customer_name / property_addr（详细地址）/ 身份证等任何 PII 邻域字段。


def _load_persona_pipeline():
    """懒加载 persona 管线模块；缺模块/依赖时返回 (None, None, None, err_msg)。"""
    try:
        # 按需挂载：只有真要重算明细时才把 tools/persona 放进 sys.path，
        # 避免模块导入期就遮蔽 stdlib/第三方同名模块。
        if os.path.isdir(_PERSONA_MOD_DIR) and _PERSONA_MOD_DIR not in sys.path:
            sys.path.insert(0, _PERSONA_MOD_DIR)
        import benchmark as _benchmark_mod  # type: ignore
        import critic as _critic_mod  # type: ignore
        import generator as _generator_mod  # type: ignore

        return _benchmark_mod, _generator_mod, _critic_mod, None
    except Exception as exc:  # noqa: BLE001
        return None, None, None, f"persona 管线模块加载失败：{exc}"


def _query_scalar(ctx, key, default=None):
    """取 query 参数的首值（标量化）。

    app.py 用 parse_qs 构造 ctx.query，值形如 ``{"max_rows": ["5"]}``——直接
    ``int(...)`` 会抛 TypeError，所以两个 evidence 接口都必须经这里取值。
    """
    q = getattr(ctx, "query", None) or {}
    v = q.get(key) if q else None
    if v is None or v == "":
        return default
    if isinstance(v, (list, tuple)):
        if not v:
            return default
        v0 = v[0]
        return default if v0 in (None, "") else v0
    return v


# 单次管线重算的样本量天花板：n 由 admin 可传，不设上限会让一次页面渲染跑超大计算。
_EVIDENCE_MAX_N = 5000


def _resolve_evidence_params(ctx):
    """解析 n / seed / max_rows，返回 (params, None)；参数非法时返回 (None, 错误说明)。

    n / seed 的缺省值一律取 persona_report.json 的 params，query 只作显式覆盖——
    两个 evidence 接口必须共用本函数，否则 JSON 与 iframe 页会用不同 n/seed 重算，
    页面上「本表与 R2-12 例卡同源：同 seed、同 n」就成了假话。
    """
    report = _load_json(_PERSONA_FILE)
    params = (report or {}).get("params") or {}
    try:
        n = int(_query_scalar(ctx, "n", params.get("n", 1000)))
        seed = int(_query_scalar(ctx, "seed", params.get("seed", 42)))
        raw_max = _query_scalar(ctx, "max_rows")
        max_rows = None if raw_max in (None, "") else int(raw_max)
    except (TypeError, ValueError):
        return None, ("n / seed / max_rows 必须是整数", "err_int", {})
    if not 1 <= n <= _EVIDENCE_MAX_N:
        return None, (f"n 必须在 1~{_EVIDENCE_MAX_N} 之间", "err_n_range", {"max": _EVIDENCE_MAX_N})
    # seed 没有上限（任意非负整数都是合法随机种子），但负数会让 numpy 抛
    # "expected non-negative integer"，若不在这里拦掉就会变成 500，
    # 还把内部异常串直接吐进 iframe 错误页。
    if seed < 0:
        return None, ("seed 必须是不小于 0 的整数", "err_seed", {})
    # 下游 _build_desensitized_rows 的 `if max_rows and max_rows > 0` 把 0 和负数当成
    # 「未设置」，不在这里拦掉的话 ?max_rows=-3 会静默返回全量行，用户拿不到任何提示。
    if max_rows is not None and max_rows < 1:
        return None, ("max_rows 必须是不小于 1 的整数", "err_max_rows", {})
    return {
        "n": n,
        "seed": seed,
        "max_rows": max_rows,
        "generated_at": (report or {}).get("generated_at"),
    }, None


def _build_desensitized_rows(n=1000, seed=42, max_rows=None):
    """用报告 params（或默认值）确定性生成 naive + calibrated，返回脱敏逐行明细。

    输出字段（违约概率与报告 example_cases 同名，特征是摊平后的列名）：
        idx: 0..n-1 的合成序号（非任何业务主键，保证不映射 PII）。
             报告 example_cases 里同义字段叫 index，两者取值一一对应——
             前端案例锚链正是用 index → idx 定位本表的行。
        industry / city: 画像的属性维度（随基准行抽样带出，校准不改写）：
             industry 为行业门类中文展示名（INDUSTRY_CATEGORIES 单点口径），
             city 为抵押物所在城市（脱敏到市级）。判读规则影响需要知道每笔
             画像的作用域归属（行业/城市/网格），仅三特征对照不出来。
        income_monthly_naive / income_monthly_cal
        debt_ratio_naive / debt_ratio_cal
        credit_score_naive / credit_score_cal
        default_prob_naive / default_prob_calibrated
        delta_pp
    三个特征在 example_cases 里是嵌套的 features_naive / features_calibrated 对象，
    本表摊平成 `<feature>_naive` / `<feature>_cal` 两列，便于逐列对照；
    违约概率沿用 example_cases 的 `_naive` / `_calibrated` 命名（与例卡共用同一套字段名）。
    行内不包含客户名/详细地址/身份证/客户号/贷款号——严格落在无 PII 白名单内。
    """
    bench_mod, gen_mod, critic_mod, err = _load_persona_pipeline()
    if err is not None:
        return None, err
    try:
        bench = bench_mod.load_benchmark()
        naive = gen_mod.generate_personas(n=n, bias="naive", seed=seed, benchmark_data=bench)
        calibrated, _trajectory = critic_mod.calibrate(bench, naive)
    except Exception as exc:  # noqa: BLE001
        return None, f"persona 管线执行失败：{exc}"

    # 行业 slug → 中文展示名：与 P6 作用域校验/前端下拉同一份清单（tools/risk/config），
    # 不允许第二份映射。db 导入失败（如纯 persona 环境直调）时回退展示 slug 原值。
    try:
        import db as _db  # noqa: PLC0415  按需导入：避免模块加载期强依赖前端连接层

        industry_names = _db.config.INDUSTRY_CATEGORIES
    except Exception:  # noqa: BLE001
        industry_names = {}

    def _industry_label(slug):
        if slug is None:
            return None
        return industry_names.get(slug, slug)

    n_actual = min(n, naive.n)
    if max_rows and max_rows > 0:
        n_actual = min(n_actual, max_rows)
    # 为避免一次性生成 1000 行过大 JSON，允许上限裁剪；但默认 n_actual==n==1000。
    inc_n = naive.income_monthly[:n_actual]
    dr_n = naive.debt_ratio[:n_actual]
    cs_n = naive.credit_score[:n_actual]
    inc_c = calibrated.income_monthly[:n_actual]
    dr_c = calibrated.debt_ratio[:n_actual]
    cs_c = calibrated.credit_score[:n_actual]
    p_n = naive.default_prob[:n_actual]
    p_c = calibrated.default_prob[:n_actual]
    ind = (calibrated.industry or naive.industry or [None] * n_actual)[:n_actual]
    city = (calibrated.city or naive.city or [None] * n_actual)[:n_actual]
    rows = []
    for i in range(n_actual):
        pn = float(p_n[i])
        pc = float(p_c[i])
        rows.append(
            {
                "idx": int(i),
                "city": city[i],
                "industry": _industry_label(ind[i]),
                "income_monthly_naive": round(float(inc_n[i]), 2),
                "income_monthly_cal": round(float(inc_c[i]), 2),
                "debt_ratio_naive": round(float(dr_n[i]), 6),
                "debt_ratio_cal": round(float(dr_c[i]), 6),
                "credit_score_naive": int(round(float(cs_n[i]))),
                "credit_score_cal": int(round(float(cs_c[i]))),
                "default_prob_naive": round(pn, 6),
                "default_prob_calibrated": round(pc, 6),
                "delta_pp": round((pc - pn) * 100, 3),  # 百分点差，便于排序展示
            }
        )
    return rows, None


def get_sandbox_evidence_rows(ctx):
    """GET /api/sandbox/evidence_rows：返回 JSON 型逐客户修正前 vs 修正后明细。

    Query 参数（均经 _query_scalar 取标量）：
        n / seed: 可选覆盖；缺省读 persona_report.json 的 params。
        max_rows: 可选裁剪输出行数（管线仍按 n 全量重算，只裁尾部）。
    rows 字段命名见 _build_desensitized_rows；features 只列三个特征名，
    每个特征在行内均有 `<feature>_naive` / `<feature>_cal` 两列。
    """
    if not _require_admin(ctx):
        return 403, {
            "error": "逐客户校准明细仅对管理员开放",
            "error_key": "err_403_admin",
            "error_params": {},
        }
    resolved, err = _resolve_evidence_params(ctx)
    if err is not None:
        err_text, err_key, err_params = err
        return 400, {"error": err_text, "error_key": err_key, "error_params": err_params}
    n, seed = resolved["n"], resolved["seed"]
    rows, err = _build_desensitized_rows(n=n, seed=seed, max_rows=resolved["max_rows"])
    if err is not None:
        return 500, {"ok": False, "error": err}
    return {
        "ok": True,
        "n": n,
        "seed": seed,
        "row_count": len(rows),
        "generated_at": resolved["generated_at"],
        "features": ["income_monthly", "debt_ratio", "credit_score"],
        "rows": rows,
    }


# iframe 独立页：纯静态 HTML + 内置表格（不依赖主页面样式，零样式污染）。
# 语言由 ?lang=zh-CN|en|zh-HK 控制（与主页面 i18n 三语一致）。
_EVIDENCE_I18N = {
    "zh-CN": {
        "title": "逐客户校准明细 · 修正前 vs 修正后（脱敏）",
        "subtitle": "所有记录均为合成模拟客户，唯一标识为序号 idx；不含客户号、姓名、详细地址、身份证等敏感字段。行业为门类名、城市仅到市级（脱敏维度），供判读规则作用域是否命中。",
        "col_idx": "序号",
        "col_city": "城市（脱敏）",
        "col_industry": "行业",
        "col_income": "月收入（修正前→后）",
        "col_debt": "负债率（修正前→后）",
        "col_score": "信用分（修正前→后）",
        "col_p_naive": "违约率 修正前",
        "col_p_cal": "违约率 修正后",
        "col_delta": "变化 (pp)",
        "filter": "筛选：",
        "filter_all": "全部",
        "filter_pos": "仅 Δ>0（被美化者）",
        "filter_neg": "仅 Δ≤0",
        "sort": "排序：",
        "sort_delta_desc": "Δ 降序（美化最重在前）",
        "sort_delta_asc": "Δ 升序",
        "sort_idx": "按序号",
        "sort_p_cal_desc": "修正后违约率 降序",
        "n_shown": "显示 {shown} / {total} 条",
        "note": "本表与 R2-12 例卡同源：同 seed、同 n；例卡仅为 3~5 条代表，本表为逐行完整对照。",
        "err_403_admin": "逐客户校准明细仅对管理员开放。如需查看请联系管理员开通。",
        "err_int": "n / seed / max_rows 必须是整数",
        "err_n_range": "n 必须在 1~{max} 之间",
        "err_seed": "seed 必须是不小于 0 的整数",
        "err_max_rows": "max_rows 必须是不小于 1 的整数",
        "err_pipe_load": "persona 管线模块加载失败：{err}",
        "err_pipe_exec": "persona 管线执行失败：{err}",
        "parse_fail": "数据解析失败：",
        "rows_omitted": " 条省略（请调整筛选/排序查看其他行）",
    },
    "en": {
        "title": "Row-level Calibration · Before vs After Correction (desensitized)",
        "subtitle": "All records are synthetic personas. The only identifier is the sequence idx; no customer IDs, names, street addresses, or national IDs are included. Industry is shown at category level and city is desensitized to city level — both support judging whether a rule's scope matches.",
        "col_idx": "Idx",
        "col_city": "City (desensitized)",
        "col_industry": "Industry",
        "col_income": "Monthly Income (before→after)",
        "col_debt": "Debt Ratio (before→after)",
        "col_score": "Credit Score (before→after)",
        "col_p_naive": "Default rate (before)",
        "col_p_cal": "Default rate (after)",
        "col_delta": "Δ (pp)",
        "filter": "Filter: ",
        "filter_all": "All",
        "filter_pos": "Only Δ>0 (beautified cases)",
        "filter_neg": "Only Δ≤0",
        "sort": "Sort: ",
        "sort_delta_desc": "Δ desc (most beautified first)",
        "sort_delta_asc": "Δ asc",
        "sort_idx": "By idx",
        "sort_p_cal_desc": "After-correction default rate desc",
        "n_shown": "Showing {shown} / {total}",
        "note": "Same seed & n as R2-12 example cards. Cards are 3~5 representatives; this table is the full row-level before/after comparison.",
        "err_403_admin": "Per-customer calibration details are admin-only. Contact an administrator for access.",
        "err_int": "n / seed / max_rows must be integers",
        "err_n_range": "n must be within 1~{max}",
        "err_seed": "seed must be a non-negative integer",
        "err_max_rows": "max_rows must be no less than 1",
        "err_pipe_load": "persona pipeline module failed to load: {err}",
        "err_pipe_exec": "persona pipeline execution failed: {err}",
        "parse_fail": "Failed to parse data: ",
        "rows_omitted": " rows omitted (adjust filter/sort to see other rows)",
    },
    "zh-HK": {
        "title": "逐客戶校準明細 · 修正前 vs 修正後（脫敏）",
        "subtitle": "所有記錄均為合成模擬客戶，唯一識別為序號 idx；不含客戶號、姓名、詳細地址、身分證等敏感欄位。行業為門類名、城市僅到市級（脫敏維度），供判讀規則作用域是否命中。",
        "col_idx": "序號",
        "col_city": "城市（脫敏）",
        "col_industry": "行業",
        "col_income": "月收入（修正前→後）",
        "col_debt": "負債率（修正前→後）",
        "col_score": "信用分（修正前→後）",
        "col_p_naive": "違約率 修正前",
        "col_p_cal": "違約率 修正後",
        "col_delta": "變化 (pp)",
        "filter": "篩選：",
        "filter_all": "全部",
        "filter_pos": "僅 Δ>0（被美化者）",
        "filter_neg": "僅 Δ≤0",
        "sort": "排序：",
        "sort_delta_desc": "Δ 降序（美化最重在前）",
        "sort_delta_asc": "Δ 升序",
        "sort_idx": "按序號",
        "sort_p_cal_desc": "修正後違約率 降序",
        "n_shown": "顯示 {shown} / {total} 條",
        "note": "本表與 R2-12 例卡同源：同 seed、同 n；例卡僅為 3~5 條代表，本表為逐行完整對照。",
        "err_403_admin": "逐客戶校準明細僅對管理員開放。如需查看請聯繫管理員開通。",
        "err_int": "n / seed / max_rows 必須是整數",
        "err_n_range": "n 必須在 1~{max} 之間",
        "err_seed": "seed 必須是不小於 0 的整數",
        "err_max_rows": "max_rows 必須是不小於 1 的整數",
        "err_pipe_load": "persona 管線模組載入失敗：{err}",
        "err_pipe_exec": "persona 管線執行失敗：{err}",
        "parse_fail": "數據解析失敗：",
        "rows_omitted": " 條省略（請調整篩選/排序查看其他行）",
    },
}


def _evidence_error_page(code, msg):
    """iframe 内的纯文案错误页：与主页面样式隔离，不引用任何外部资源。"""
    return (
        "<!doctype html><html><body style='font-family:sans-serif'>"
        f"<h3>{code}</h3><p>{html.escape(msg)}</p></body></html>"
    )


def get_sandbox_evidence_page(ctx):
    """GET /api/sandbox/evidence_page：返回独立 HTML，用于前端 iframe 嵌入。

    该页自包含（不依赖主页面 CSS/JS），同源直接加载即可渲染（前端 iframe 故意不设
    sandbox 属性——加了会断掉与主页面之间的 postMessage 行定位联动）：
    1. 后端一次注入数据（<script id=data>），避免二次跨 iframe fetch；
    2. 原生 JS 实现本地筛选/排序，最多渲染 1000 行（见模板内 LIMIT），其余提示省略；
    3. 零 PII 输出（白名单字段见 _build_desensitized_rows）。
    """
    lang = "zh-CN"
    raw_lang = _query_scalar(ctx, "lang")
    if raw_lang in _EVIDENCE_I18N:
        lang = raw_lang
    t = _EVIDENCE_I18N[lang]
    if not _require_admin(ctx):
        return (
            403,
            {"__html__": _evidence_error_page("403 Forbidden", t["err_403_admin"])},
        )
    resolved, err = _resolve_evidence_params(ctx)
    if err is not None:
        err_text, err_key, err_params = err
        msg = t.get(err_key, err_text).format(**err_params)
        return 400, {"__html__": _evidence_error_page("400 Bad Request", msg)}
    rows_payload, pipe_err = _build_desensitized_rows(
        n=resolved["n"], seed=resolved["seed"], max_rows=resolved["max_rows"]
    )
    if pipe_err is not None:
        # 管线错误前缀按语言翻译（异常明细原样附后，属服务端诊断信息）
        if pipe_err.startswith("persona 管线模块加载失败："):
            msg = t["err_pipe_load"].format(err=pipe_err.split("：", 1)[1])
        elif pipe_err.startswith("persona 管线执行失败："):
            msg = t["err_pipe_exec"].format(err=pipe_err.split("：", 1)[1])
        else:
            msg = pipe_err
        return 500, {"__html__": _evidence_error_page("500 Server Error", msg)}
    data_json = json.dumps(
        {"rows": rows_payload, "lang": lang, "t": t}, ensure_ascii=False
    ).replace("<", "\\u003c")
    t_json = json.dumps(t, ensure_ascii=False).replace("<", "\\u003c")
    # 先替换文案占位、最后注入数据：反过来时若数据里恰好含占位串会被二次替换。
    page = (
        _EVIDENCE_PAGE_TEMPLATE.replace("__LANG__", lang)
        .replace("__TITLE__", html.escape(t["title"]))
        .replace("__T_JSON__", t_json)
        .replace("__DATA_JSON__", data_json)
    )
    return 200, {"__html__": page}


_EVIDENCE_PAGE_TEMPLATE = """<!doctype html>
<html lang="__LANG__">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>__TITLE__</title>
<style>
  :root{
    --bg:#fff; --fg:#111827; --muted:#6b7280; --border:#e5e7eb; --bad:#dc2626; --ok:#16a34a;
    --head:#f9fafb; --row-alt:#fafafa; --link:#2563eb;
  }
  *{box-sizing:border-box}
  body{margin:0;padding:14px 16px;background:var(--bg);color:var(--fg);
       font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",PingFang SC,Microsoft YaHei,sans-serif;}
  h1{font-size:15px;margin:0 0 4px}
  .sub{color:var(--muted);font-size:12px;margin-bottom:10px}
  .bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0 10px}
  .bar select,.bar label{font-size:12px}
  .count{color:var(--muted);font-size:12px;margin-left:auto}
  table{width:100%;border-collapse:collapse;font-size:12px}
  thead th{background:var(--head);border-bottom:1px solid var(--border);
           text-align:left;padding:6px 8px;position:sticky;top:0;z-index:1}
  tbody td{padding:5px 8px;border-bottom:1px solid var(--border);vertical-align:top}
  tbody tr:nth-child(even) td{background:var(--row-alt)}
  /* 斑马纹的 td 背景不透明，高亮只写在 tr 上会被盖住（偶数行点了看不见）→ 必须命中 td */
  tr.row-hl > td{background:#fde68a !important}
  .num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  .mono{font-variant-numeric:tabular-nums;white-space:nowrap}
  .delta-pos{color:var(--bad);font-weight:600}
  .delta-zero{color:var(--muted)}
  .arrow{color:var(--muted);margin:0 4px}
  .before{color:#92400e}
  .after{color:#065f46;font-weight:600}
  .note{color:var(--muted);font-size:11px;margin-top:8px}
</style>
</head>
<body>
<h1 id="t__title"></h1>
<div class="sub" id="t__subtitle"></div>
<div class="bar">
  <label id="t__filter" for="f-filter"></label>
  <select id="f-filter">
    <option value="all" id="t__filter_all"></option>
    <option value="pos" id="t__filter_pos"></option>
    <option value="neg" id="t__filter_neg"></option>
  </select>
  <label id="t__sort" for="f-sort"></label>
  <select id="f-sort">
    <option value="delta_desc" id="t__sort_delta_desc"></option>
    <option value="delta_asc" id="t__sort_delta_asc"></option>
    <option value="idx" id="t__sort_idx" selected></option>
    <option value="p_cal_desc" id="t__sort_p_cal_desc"></option>
  </select>
  <div class="count" id="t__n_shown"></div>
</div>
<div style="max-height:520px;overflow:auto;border:1px solid var(--border);border-radius:6px">
  <table id="tbl">
    <thead><tr>
      <th class="num" id="t__col_idx"></th>
      <th id="t__col_city"></th>
      <th id="t__col_industry"></th>
      <th id="t__col_income"></th>
      <th id="t__col_debt"></th>
      <th id="t__col_score"></th>
      <th class="num" id="t__col_p_naive"></th>
      <th class="num" id="t__col_p_cal"></th>
      <th class="num" id="t__col_delta"></th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
</div>
<div class="note" id="t__note"></div>

<script id="i18n" type="application/json">__T_JSON__</script>
<script id="data" type="application/json">__DATA_JSON__</script>
<script>
(function(){
  var T = {}; try { T = JSON.parse(document.getElementById('i18n').textContent); } catch(e) {}
  var RAW; try { RAW = JSON.parse(document.getElementById('data').textContent); }
  catch(e){ document.getElementById('tbody').innerHTML =
    '<tr><td colspan=9>'+(T.parse_fail||'数据解析失败：')+String(e.message).replace(/[<>&]/g, '')+'</td></tr>'; return; }
  var rows = RAW.rows || [];
  var t = RAW.t || T;
  // 所有待翻译元素统一约定 id="t__<dictKey>"：dictKey 与 _EVIDENCE_I18N 字典键逐字一致。
  function applyI18n(){
    var nodes = document.querySelectorAll('[id^="t__"]');
    for(var i=0;i<nodes.length;i++){
      var id = nodes[i].id;
      var key = id.substring(3);
      if(t[key] == null) continue;
      try { nodes[i].textContent = String(t[key]); }
      catch(_){ try { nodes[i].innerText = String(t[key]); } catch(_e){} }
    }
  }
  applyI18n();
  document.title = t.title || '';
  // 千分位跟随页面语言，别硬编码 zh-CN（英文模式下用中文 locale 是错的）。
  var LOCALE = (RAW.lang === 'en') ? 'en-US' : (RAW.lang === 'zh-HK' ? 'zh-HK' : 'zh-CN');
  var fmtInc = function(v){ return Math.round(v).toLocaleString(LOCALE); };
  var fmtDebt = function(v){ return Math.round(Number(v)*1000)/10 + '%'; };
  var fmtScore = function(v){ return Math.round(Number(v)).toLocaleString(LOCALE); };
  var fmtP = function(v){ return (Number(v)*100).toFixed(2) + '%'; };
  var fmtDelta = function(v){
    var s = (v>=0?'+':'') + (Math.round(v*10)/10).toFixed(1) + 'pp';
    var cls = v>0 ? 'delta-pos' : 'delta-zero';
    return '<span class="'+cls+'">'+s+'</span>';
  };
  function render(){
    var f = document.getElementById('f-filter').value;
    var s = document.getElementById('f-sort').value;
    var arr = rows.slice();
    if(f==='pos') arr = arr.filter(function(r){return r.delta_pp>0});
    else if(f==='neg') arr = arr.filter(function(r){return r.delta_pp<=0});
    if(s==='delta_desc') arr.sort(function(a,b){return b.delta_pp-a.delta_pp || a.idx-b.idx});
    else if(s==='delta_asc') arr.sort(function(a,b){return a.delta_pp-b.delta_pp || a.idx-b.idx});
    else if(s==='idx') arr.sort(function(a,b){return a.idx-b.idx});
    else if(s==='p_cal_desc') arr.sort(function(a,b){return b.default_prob_calibrated-a.default_prob_calibrated || a.idx-b.idx});
    var total = rows.length, shown = arr.length;
    var cntTpl = t.n_shown || 'Showing {shown} / {total}';
    var cntEl = document.getElementById('t__n_shown');
    if(cntEl){ try { cntEl.textContent = cntTpl.replace('{shown}', String(shown)).replace('{total}', String(total)); } catch(_){} }
    var LIMIT = 1000;
    var slice = arr.slice(0, LIMIT);
    var hb = '';
    var esc = function(v){ return String(v==null?'':v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); };
    for(var i=0;i<slice.length;i++){
      var r = slice[i];
      hb += '<tr data-idx="'+r.idx+'">' +
        '<td class="num mono">'+r.idx+'</td>' +
        '<td class="mono">'+(r.city!=null ? esc(r.city) : '-')+'</td>' +
        '<td>'+(r.industry!=null ? esc(r.industry) : '-')+'</td>' +
        '<td><span class="before">'+fmtInc(r.income_monthly_naive)+'</span><span class="arrow">→</span><span class="after">'+fmtInc(r.income_monthly_cal)+'</span></td>' +
        '<td><span class="before">'+fmtDebt(r.debt_ratio_naive)+'</span><span class="arrow">→</span><span class="after">'+fmtDebt(r.debt_ratio_cal)+'</span></td>' +
        '<td><span class="before">'+fmtScore(r.credit_score_naive)+'</span><span class="arrow">→</span><span class="after">'+fmtScore(r.credit_score_cal)+'</span></td>' +
        '<td class="num mono before">'+fmtP(r.default_prob_naive)+'</td>' +
        '<td class="num mono after">'+fmtP(r.default_prob_calibrated)+'</td>' +
        '<td class="num mono">'+fmtDelta(r.delta_pp)+'</td>' +
        '</tr>';
    }
    if(slice.length < shown){
      hb += '<tr><td colspan=9 style="color:var(--muted);text-align:center;padding:8px">' +
        (shown - slice.length) + (T.rows_omitted||' 条省略（请调整筛选/排序查看其他行）') + '</td></tr>';
    }
    document.getElementById('tbody').innerHTML = hb;
  }
  // 暴露给外部（主要是 postMessage）：主页面 postMessage 会在 IIFE 之后调用，
  // 因此把 render / 公共方法挂 window，避免 IIFE 内部闭包逃逸失败。
  window.__evidenceRender = render;
  document.getElementById('f-filter').addEventListener('change', render);
  document.getElementById('f-sort').addEventListener('change', render);
  // 主页面案例锚链跳转：父窗口 postMessage {type:'p6:goto-idx', idx:N} → 切到按序号排序并滚到目标行。
  if(window.addEventListener){
    window.addEventListener('message', function(ev){
      // 只认同源消息：主页面一侧已用 window.location.origin 作 targetOrigin，
      // 这里对称校验 ev.origin，避免任意站点嵌进本页就能驱动滚动与高亮。
      if(ev.origin !== window.location.origin) return;
      var d = ev.data || {};
      if(d.type !== 'p6:goto-idx' || typeof d.idx !== 'number') return;
      var sel = document.getElementById('f-sort');
      if(sel){ sel.value = 'idx'; }
      try { window.__evidenceRender(); } catch(_e){}
      var target = document.querySelector('tr[data-idx="'+d.idx+'"]');
      if(target){
        // 高亮必须走 .row-hl > td：tr 上的内联背景会被 td 的斑马纹背景盖住。
        try { target.classList.add('row-hl'); } catch(_){}
        setTimeout(function(){ try { target.classList.remove('row-hl'); } catch(_){} }, 2400);
        try { target.scrollIntoView({behavior:'smooth', block:'center'}); } catch(_){}
      }
    }, false);
  }
  render();
})();
</script>
</body>
</html>
"""
