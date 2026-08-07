#!/usr/bin/env python
"""P7 · AVM 估值管理（US-05 / R-STA-02 / R-UNW-03 / R-UBQ-01 / AC-07）。

页面回答三个问题：
1. **模型现在什么水平**——训练产物 output/avm/avm_report.json 的真实指标，
   与 AC-07 目标（MAPE ≤ 10%）并排展示。当前 15.508% 未达标，页面直接标红：
   作品集里把差距连同归因一起摆出来，比藏起来更有说服力。
2. **哪些估值不可信**——R-UNW-03：|AVM 估值 − true_market_price| / true_market_price > 30%
   标记异常，200 笔里 76 笔（38%）中招。列表 + 明细支持逐笔人工核查。
3. **偏差从哪来**——按城市/五级分类拆异常率，并额外算「有符号偏差」（abs 偏差看不出
   方向），用于暴露某个城市系统性高估/低估。DWS 只存 abs 偏差，方向须在这里用
   market_valuation 与业务库 true_market_price 现算。
4. **38% 到底该记在谁头上**——「异常估值归因」区块（/api/avm/attribution）把这 38%
   拆成三个可加分量并给出反事实异常率，见下方 _attribution_payload 的注释。

口径声明（页面上也会明示，避免误读）：
- `dws_risk_class.valuation_deviation_pct` 是**小数**（0.5529 = 55.29%），不是百分数；
  仅 AVM 命中行非空（见 tools/risk/risk_engine.py 的注释：回退链与基准同源，偏差恒 0，
  算了只会稀释异常信号），因此「AVM 覆盖率」= 偏差非空行占比。
- 偏差基准 true_market_price 是**合成种子数据**，不是网签成交价：它由 seed 脚本用
  「2026-08-04 版 AVM 模型的隐含单价 × U(0.75,1.35)」生成，所以 R-UNW-03 实际度量的是
  「当前模型 vs 模型自己的旧快照」，页面必须把这一点讲透，否则读者会把它读成估值准确性。

数据来源：spacefin_crawler.dws_risk_class（200 行明细）、ads_risk_valuation_alerts（76 条
人工核查告警）、business.collateral（地址/基准价，取城市维度）、output/avm/*/avm_report.json、
output/avm/bias_diag/（离线偏差诊断产物）、seed/generate_seed.py（冻结基准常量，AST 读取）。
"""

import ast
import json
import os
import statistics
import sys

# pages 以包形式被导入时 frontend 根目录不一定在 sys.path 上（取决于启动方式），
# 这里补一次，保证 `import db` 在 app.py 直跑与 `python -c "import pages"` 两种场景都成立。
_FRONTEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _FRONTEND_DIR not in sys.path:
    sys.path.insert(0, _FRONTEND_DIR)

import db  # noqa: E402

# 仓库根：本文件在 <root>/tools/frontend/pages/ 下，回退四层。
# 训练产物在 output/ 下（.gitignore），不是数据库表，只能按路径 json.load 读。
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
_AVM_OUT_DIR = os.path.join(_REPO_ROOT, "output", "avm")
# 离线偏差诊断产物目录：rows.json 是逐行三分量分解，attribution.json 是 76 笔异常的归因分类。
# 两者都不进库（分析中间产物），页面只读不写；缺失时对应模块降级为「产物缺失」。
_BIAS_DIAG_DIR = os.path.join(_AVM_OUT_DIR, "bias_diag")
# 冻结基准的原始出处。用 AST 读常量而不是 import：seed 脚本带 __main__ 副作用与额外依赖，
# 前端进程不该为了拿两个字典去执行它；literal_eval 也保证这里不会被脚本里的代码影响。
_SEED_FILE = os.path.join(_REPO_ROOT, "seed", "generate_seed.py")

# R-UNW-03 阈值：与 tools/risk/config.VALUATION_DEVIATION_THRESHOLD 同值。
# 这里不 import risk.config —— 前端只做展示，硬耦合风险引擎配置会让页面在
# 无风险引擎依赖的环境下加载失败；数值不一致的风险由页面上标注阈值来源兜底。
DEVIATION_THRESHOLD = 0.30
# AC-07 验收目标：MAPE ≤ 10%。
AC07_TARGET_MAPE = 10.0

PAGE_SIZE = 20

# 绝对偏差分桶：30% 以上的桶在前端标红（R-UNW-03 异常区）。
# 桶边界在 0.30 处刻意断开，保证「恰好 30% 不算异常」（B-04 口径）与分桶视觉一致。
_DEV_BUCKETS = [
    ("0-5%", 0.0, 0.05),
    ("5-10%", 0.05, 0.10),
    ("10-20%", 0.10, 0.20),
    ("20-30%", 0.20, 0.30),
    ("30-50%", 0.30, 0.50),
    ("50-80%", 0.50, 0.80),
    ("80-100%", 0.80, 1.00),
    (">100%", 1.00, float("inf")),
]


# ---------------- 训练产物读取（R-UBQ-01 血缘） ----------------


def _load_json(path):
    """读一个 JSON 产物；文件缺失/损坏返回 None（页面降级展示，不 500）。

    训练报告与偏差诊断产物都在 output/ 下（.gitignore），换台机器 clone 完就是没有的，
    因此「读不到」是正常路径而非异常路径，绝不能让它冒泡成 500。
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _report_digest(report, fallback_version):
    """把 avm_report.json 压成血缘表一行。

    历史产物（如 b06_0）在加 version 字段之前生成，没有 version 键——
    此时用目录名兜底，血缘表宁可显示 'b06_0' 也不能断链。
    """
    m = (report or {}).get("metrics", {})
    model = m.get("model_total_price", {})
    base = m.get("baseline_median_x_area", {})
    return {
        "version": report.get("version") or fallback_version,
        "source": fallback_version,
        "trained_at": report.get("trained_at"),
        "algo": report.get("model"),
        "n_train": report.get("n_train"),
        "n_test": report.get("n_test"),
        "mape": model.get("mape"),
        "mdape": model.get("mdape"),
        "r2": model.get("r2"),
        "baseline_mape": base.get("mape"),
        "improvement_pct": m.get("mape_relative_improvement_pct"),
    }


def _lineage():
    """扫描 output/avm/ 下所有 avm_report.json，按训练时间倒序，构成版本血缘。

    根目录的是当前在跑的版本（风险引擎读的 model.joblib 与它同批产出），
    子目录是历史留档。多版本并列才能体现 R-UBQ-01「结论可追溯到具体模型版本」。
    """
    items = []
    cur_path = os.path.join(_AVM_OUT_DIR, "avm_report.json")
    cur = _load_json(cur_path)
    if cur:
        d = _report_digest(cur, "current")
        d["is_current"] = True
        items.append(d)
    if os.path.isdir(_AVM_OUT_DIR):
        for name in sorted(os.listdir(_AVM_OUT_DIR)):
            sub = os.path.join(_AVM_OUT_DIR, name, "avm_report.json")
            if not os.path.isfile(sub):
                continue
            rep = _load_json(sub)
            if not rep:
                continue
            d = _report_digest(rep, name)
            d["is_current"] = False
            items.append(d)
    items.sort(key=lambda x: x["trained_at"] or "", reverse=True)
    # 相对上一版（时间上更早的那版）的 MAPE 变化，负数=变好。
    for i, it in enumerate(items):
        prev = items[i + 1] if i + 1 < len(items) else None
        if prev and it["mape"] is not None and prev["mape"] is not None:
            it["mape_delta"] = round(it["mape"] - prev["mape"], 3)
        else:
            it["mape_delta"] = None
    return items


def _model_card(report):
    """模型卡片：算法/版本/样本量/指标 + AC-07 达标判定。"""
    if not report:
        return None
    m = report.get("metrics", {})
    model = m.get("model_total_price", {})
    base = m.get("baseline_median_x_area", {})
    notes = report.get("data_notes", {})
    mape = model.get("mape")
    return {
        "version": report.get("version") or "unknown",
        "algo": report.get("model"),
        "trained_at": report.get("trained_at"),
        "seed": report.get("seed"),
        "n_train": report.get("n_train"),
        "n_test": report.get("n_test"),
        "target": report.get("target"),
        "leakage_control": report.get("leakage_control"),
        "mape": mape,
        "mdape": model.get("mdape"),
        "r2": model.get("r2"),
        "baseline": {
            "mape": base.get("mape"),
            "mdape": base.get("mdape"),
            "r2": base.get("r2"),
        },
        "improvement_pct": m.get("mape_relative_improvement_pct"),
        "params": report.get("model_params", {}),
        "feature_count": len(report.get("feature_names") or []),
        "feature_names": report.get("feature_names") or [],
        "coord_rows_pct": notes.get("coord_rows_pct"),
        "community_missing_pct": notes.get("community_missing_pct"),
        "rows_after_clean": notes.get("rows_after_clean"),
        # AC-07：目标与实测并排，gap 为正表示还差多少个百分点。
        "ac07": {
            "target_mape": AC07_TARGET_MAPE,
            "actual_mape": mape,
            "passed": bool(mape is not None and mape <= AC07_TARGET_MAPE),
            "gap": round(mape - AC07_TARGET_MAPE, 3) if mape is not None else None,
        },
        # AC-07 收口口径：精度 @ 覆盖率。置信分只用训练集统计（无泄漏）。
        # 报告里有 confidence 块才有这些字段；旧产物（r4 及更早）没有 → 前端优雅降级。
        "coverage": _coverage_card(report),
    }


def _coverage_card(report):
    """从报告 confidence 块提取覆盖率信息；旧产物无此块时返回 None。"""
    conf = (report or {}).get("confidence") or {}
    if not conf:
        return None
    ac = conf.get("ac07_coverage_at_10pct")
    curve = conf.get("coverage_curve") or []
    tiers = conf.get("tiers") or {}
    return {
        "method": conf.get("method"),
        "tiers": tiers,
        "curve": curve,
        # 达标覆盖率：MAPE 首次 ≤10% 的最大覆盖（最小弃权）。None = 30% 以上均不可达。
        "ac07": (
            {
                "coverage_pct": ac["coverage_pct"],
                "mape": ac["mape"],
                "n": ac["n"],
            }
            if ac
            else None
        ),
        "note": conf.get("note"),
    }


def _error_decomposition(report):
    """训练侧误差分解（按位置信号完整度 / 按城市），解释 MAPE 为什么停在 15.5%。"""
    dec = (report or {}).get("error_decomposition", {})
    seg_label = {
        "has_comm_coord": "有小区 + 有坐标",
        "has_comm_only": "有小区、无坐标",
        "no_comm": "无小区（title 也解析不出）",
    }
    by_segment = [
        {
            "segment": seg_label.get(k, k),
            "key": k,
            "n": v.get("n"),
            "mape": v.get("mape"),
            "mdape": v.get("mdape"),
            "weight_pct": v.get("weight_pct"),
        }
        for k, v in (dec.get("by_segment") or {}).items()
    ]
    by_city = [
        {"city_code": k, "n": v.get("n"), "mape": v.get("mape")}
        for k, v in (dec.get("by_city_mape") or {}).items()
    ]
    by_city.sort(key=lambda x: -(x["mape"] or 0))
    return {"by_segment": by_segment, "by_city": by_city}


# ---------------- 组合侧（数据库）取数 ----------------


def _fetch_portfolio_rows():
    """拉 200 行估值明细并 join 业务库抵押物（地址=城市维度，true_market_price=偏差基准）。

    为什么在 Python 里 join 而不是 SQL join：dws_risk_class 在房产库、collateral 在业务库，
    是两个物理库两个连接（见 db.crawl_conn / db.biz_conn），跨库 join 只能在应用层做。
    200 行级数据一次性取回，成本可忽略。
    """
    crawl = db.crawl_conn()
    biz = db.biz_conn()
    try:
        cur = crawl.cursor()
        cur.execute(
            "SELECT loan_id, customer_id, collateral_id, balance, market_valuation, ltv, "
            "risk_class, low_confidence, valuation_deviation_pct, abnormal_valuation, "
            "model_version FROM dws_risk_class"
        )
        raw = cur.fetchall()
        cur.close()

        cids = [r[2] for r in raw if r[2] is not None]
        coll = {}
        if cids:
            bcur = biz.cursor()
            placeholders = ",".join(["%s"] * len(cids))
            bcur.execute(
                "SELECT collateral_id, property_addr, area, true_market_price "
                f"FROM collateral WHERE collateral_id IN ({placeholders})",
                cids,
            )
            coll = {r[0]: r for r in bcur.fetchall()}
            bcur.close()
    finally:
        crawl.close()
        biz.close()

    rows = []
    for (
        loan_id,
        customer_id,
        cid,
        balance,
        mv,
        ltv,
        risk_class,
        low_conf,
        dev,
        abnormal,
        version,
    ) in raw:
        c = coll.get(cid)
        addr = c[1] if c else None
        area = float(c[2]) if c and c[2] is not None else None
        book = float(c[3]) if c and c[3] is not None else None
        mv_f = float(mv) if mv is not None else None
        dev_f = float(dev) if dev is not None else None
        # 有符号偏差：正=AVM 高估，负=低估。DWS 只落 abs 值，方向信息在这里补回来。
        signed = None
        if mv_f is not None and book:
            signed = round((mv_f - book) / book, 4)
        rows.append(
            {
                "loan_id": loan_id,
                "customer_id": customer_id,
                "collateral_id": cid,
                "balance": float(balance) if balance is not None else None,
                "avm_valuation": mv_f,
                "book_price": book,
                "area": area,
                "ltv": float(ltv) if ltv is not None else None,
                "risk_class": risk_class,
                "low_confidence": bool(low_conf),
                "deviation_pct": dev_f,
                "signed_deviation_pct": signed,
                "abnormal_valuation": bool(abnormal),
                "model_version": version or "unknown",
                "property_addr": addr or "未标注",
                "city": _city_of(addr),
                "source_tier": _source_tier(dev_f, mv_f, book),
            }
        )
    return rows


def _city_of(addr):
    """地址取「XX市」。业务库地址是「广州市黄埔区…」格式，取到「市」为止即可。"""
    if not addr:
        return "未标注"
    i = addr.find("市")
    return addr[: i + 1] if i > 0 else "未标注"


def _source_tier(dev, mv, book):
    """判定这笔估值来自三级回退链（tools/risk/valuation.py）的哪一级。

    DWS 没落 avm_hit/dwd_hit 标志位，只能反推——依据是风险引擎的口径约定：
    偏差仅在 AVM 命中时计算（非空 ⇒ AVM）；其余情况若估值与业务库基准价相等，
    说明走到了最后一级兜底，否则是 DWD 行情。反推口径写在这里，页面上也注明。
    """
    if dev is not None:
        return "AVM 模型"
    if mv is None:
        return "无估值"
    if book is not None and abs(mv - book) < 0.01:
        return "true_market_price 兜底"
    return "DWD 行情"


def _pct_bucket(dev):
    for label, lo, hi in _DEV_BUCKETS:
        if lo <= dev < hi:
            return label
    return _DEV_BUCKETS[-1][0]


def _dimension_stats(rows, key_fn, key_name):
    """按某维度聚合异常率 + 中位有符号偏差。

    中位有符号偏差是这张表的重点：abs 偏差只能说「偏得多」，有符号偏差能说
    「整体系统性高估还是低估」——某城市 20 笔全是 -45%，那大概率不是随机误差
    而是该城市训练数据/位置信号的结构性问题。
    """
    agg = {}
    for r in rows:
        if r["deviation_pct"] is None:
            continue  # 非 AVM 行没有偏差口径，参与统计会稀释分母
        k = key_fn(r)
        a = agg.setdefault(k, {"n": 0, "abnormal": 0, "abs": [], "signed": []})
        a["n"] += 1
        a["abnormal"] += 1 if r["abnormal_valuation"] else 0
        a["abs"].append(r["deviation_pct"])
        if r["signed_deviation_pct"] is not None:
            a["signed"].append(r["signed_deviation_pct"])
    out = []
    for k, a in agg.items():
        out.append(
            {
                key_name: k,
                "n": a["n"],
                "abnormal": a["abnormal"],
                "abnormal_rate": round(a["abnormal"] / a["n"], 4) if a["n"] else 0.0,
                "median_abs_dev": round(statistics.median(a["abs"]), 4) if a["abs"] else None,
                "median_signed_dev": (
                    round(statistics.median(a["signed"]), 4) if a["signed"] else None
                ),
            }
        )
    out.sort(key=lambda x: (-x["abnormal_rate"], -x["n"]))
    return out


# ---------------- 异常估值归因（R-UNW-03 深度诊断） ----------------
#
# 这一段回答的是「38% 异常率该记在谁头上」。核心事实：偏差基准 true_market_price
# 并非市场成交价，而是 seed 脚本用 2026-08-04 版本模型的隐含单价合成出来的：
#
#     true_market_price = CITY_UNIT_PRICE[city] × U(0.75, 1.35) × area
#
# 于是 AVM 与基准之比在 log 域可加地拆成三个分量（离线诊断产物给出逐行取值）：
#
#     log(AVM / true_market) = A1 + A2 + B
#       A1 = 城市系统性错位（当前模型的城市水平 vs 冻结在基准里的旧模型城市水平）
#       A2 = 模型对面积/房龄/坐标等行内特征的响应
#       B  = seed 合成噪声，= -log(u)，u ~ U(0.75, 1.35)，与贷款质量无关
#
# 页面据此给出「反事实异常率」：把某些分量置零后重算 |exp(·) − 1| > 30% 的比例，
# 用来回答「如果基准不是合成的、模型完美命中冻结基准，还会剩多少异常」。
#
# 硬性约定：本模块不硬编码任何结论数字。方差/反事实/归因构成全部由 bias_diag 产物
# 现算，冻结基准从 seed 源码 AST 读，异常率与城市对比走实时库，产物缺失就降级。

_E = 2.718281828459045

# 反事实口径：(键, 展示名, 参与求和的分量, 解释)
_COUNTERFACTUAL_SPECS = [
    (
        "actual",
        "实际（A1+A2+B）",
        ("A1", "A2", "B"),
        "线上 R-UNW-03 判定，与库内 abnormal_valuation 一致",
    ),
    ("a1_only", "仅城市系统性错位 A1", ("A1",), "抹掉行内响应与合成噪声，只留城市水平差"),
    ("a2_b", "分城市校正后（A2+B）", ("A2", "B"), "假设逐城对齐了基准的城市水平"),
    (
        "b_only",
        "模型完美命中冻结基准（仅 B）",
        ("B",),
        "模型零误差时，纯 seed 合成噪声仍造成的异常",
    ),
    ("a2_only", "仅模型自身响应 A2", ("A2",), "真正属于模型的那部分误差"),
]

_VARIANCE_LABELS = {
    "A1": ("A1 · 城市系统性错位", "当前模型城市水平 vs 冻结基准里的旧模型城市水平"),
    "A2": ("A2 · 模型行内响应", "面积 / 房龄 / 坐标等特征带来的行间差异"),
    "B": ("B · seed 合成噪声", "true_market_price 里的 U(0.75,1.35) 乘子，与模型无关"),
}


def _seed_constants():
    """AST 解析 seed/generate_seed.py，取回冻结基准常量及其出处注释。

    取三样东西：
      - CITY_UNIT_PRICE：冻结基准单价，页面上「基准侧」对比的原始数字；
      - CITIES：城市名 ↔ 城市码映射（库里地址是中文市名，产物里是城市码）；
      - RISK_CLASSES：五级分类的规范顺序（避免前端按字典序把「损失」排到前面）。

    另外把 CITY_UNIT_PRICE 上方那段注释原样抓出来——它自述了基准取自哪一版模型，
    是「基准自指」这个结论最硬的证据，页面上直接引原文比我们复述可信得多。

    用 AST 而不是 import：seed 脚本有 __main__ 副作用和额外依赖，前端进程不该
    为了拿两个字典去执行它；literal_eval 也保证这里只取字面量、不执行任何代码。
    """
    try:
        with open(_SEED_FILE, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return None

    wanted = {"CITY_UNIT_PRICE", "CITIES", "RISK_CLASSES"}
    got, span = {}, {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not node.targets:
            continue
        tgt = node.targets[0]
        if not isinstance(tgt, ast.Name) or tgt.id not in wanted:
            continue
        try:
            got[tgt.id] = ast.literal_eval(node.value)
        except ValueError:
            continue
        span[tgt.id] = (node.lineno, node.end_lineno)
    if "CITY_UNIT_PRICE" not in got:
        return None

    # 往上回溯连续的 # 注释行，作为基准出处的原文引用。
    lines = src.splitlines()
    start, end = span["CITY_UNIT_PRICE"]
    i = start - 1  # 0-based 下标，指向常量赋值那一行
    while i > 0 and lines[i - 1].lstrip().startswith("#"):
        i -= 1
    provenance = "\n".join(ln.lstrip("# ").rstrip() for ln in lines[i : start - 1])

    name2code = {}
    for item in got.get("CITIES") or []:
        if len(item) >= 2:
            name2code[str(item[0]) + "市"] = str(item[1])

    return {
        "unit_price": got["CITY_UNIT_PRICE"],
        "name_to_code": name2code,
        "code_to_name": {v: k for k, v in name2code.items()},
        "risk_classes": list(got.get("RISK_CLASSES") or []),
        "provenance": provenance,
        "source_ref": f"seed/generate_seed.py:{i + 1}-{end}",
    }


def _diag_rows():
    """读逐行三分量分解产物 output/avm/bias_diag/rows.json。

    只保留字段齐全的行——产物是离线跑出来的，缺字段说明版本对不上，
    与其算出一个静悄悄错掉的方差，不如排除这些行并在页面上暴露行数。
    """
    raw = _load_json(os.path.join(_BIAS_DIAG_DIR, "rows.json"))
    if not isinstance(raw, list):
        return None
    rows = [
        r
        for r in raw
        if isinstance(r, dict) and all(r.get(k) is not None for k in ("A1", "A2", "B", "logdev"))
    ]
    return rows or None


def _variance_shares(diag):
    """方差贡献：Var(A_i) / Var(logdev)。

    三分量并不正交（A1 与 A2 同出于一个模型），份额之和不等于 100%——
    这是实事求是的口径，页面上会注明「分量相关，合计 ≠ 100%」，不归一化粉饰。
    """
    total = statistics.pvariance([r["logdev"] for r in diag])
    if total <= 0:
        return None
    comps = []
    for key in ("A1", "A2", "B"):
        v = statistics.pvariance([r[key] for r in diag])
        label, desc = _VARIANCE_LABELS[key]
        comps.append(
            {
                "component": key,
                "label": label,
                "desc": desc,
                "variance": round(v, 6),
                "share": round(v / total, 4),
            }
        )
    comps.sort(key=lambda x: -x["share"])
    return {
        "total_variance": round(total, 6),
        "components": comps,
        "share_sum": round(sum(c["share"] for c in comps), 4),
    }


def _counterfactuals(diag):
    """反事实异常率：只保留指定分量时，仍会被判 R-UNW-03 异常的比例。

    偏差是比值口径、分量在 log 域可加，所以「只留某几个分量」= |exp(分量之和) − 1|。
    """
    n = len(diag)
    out = []
    for key, label, comps, note in _COUNTERFACTUAL_SPECS:
        cnt = sum(
            1 for r in diag if abs(_E ** sum(r[c] for c in comps) - 1.0) > DEVIATION_THRESHOLD
        )
        out.append(
            {
                "key": key,
                "label": label,
                "components": list(comps),
                "n": cnt,
                "rate": round(cnt / n, 4) if n else 0.0,
                "note": note,
                # 「地板」= 模型完美命中冻结基准后仍消不掉的部分，纯合成噪声造成
                "is_floor": key == "b_only",
            }
        )
    return out


def _noise_gradient(diag, class_order):
    """合成噪声分量 B 在五级分类上的分布——LTV 反馈环的证据。

    B 由 seed 的 U(0.75,1.35) 独立抽样，构造上与贷款质量零相关；它若在五级分类间
    仍呈现量级梯度，唯一解释是 LTV = balance / AVM 估值 把同一个噪声读了第二遍
    （balance 也由 true_market × 噪声生成），即分类结果反过来被噪声决定。
    """
    agg = {}
    for r in diag:
        a = agg.setdefault(r.get("risk_class") or "未知", {"B": [], "n": 0, "abn": 0})
        a["B"].append(r["B"])
        a["n"] += 1
        a["abn"] += 1 if r.get("abn") else 0
    order = {c: i for i, c in enumerate(class_order or [])}
    out = [
        {
            "risk_class": k,
            "n": a["n"],
            "median_noise": round(statistics.median(a["B"]), 4),
            "abnormal_rate": round(a["abn"] / a["n"], 4) if a["n"] else 0.0,
        }
        for k, a in agg.items()
    ]
    out.sort(key=lambda x: order.get(x["risk_class"], 99))
    return out


def _attribution_mix(n_abnormal):
    """76 笔异常的归因构成，读离线产物 bias_diag/attribution.json。

    这份分类是带人工判定阈值的产物（「哪几个分量算有实质贡献」需要定线），
    页面侧重算必然与离线口径漂移，所以这里只读不算：产物在就渲染，不在就降级。
    接受三种格式：
      {"categories": [...], "rule": ...}        # 通用 categories 格式
      [{"loan_id":.., "cat":.., "cat_label":.., "side":..}, ...]  # 逐行清单，现场汇总
      {"summary": [...], "rule": ...}           # bias_attribution.py 的落盘格式
    side 取值约定：baseline=归因于合成基准，model=归因于模型本身，mixed=多因叠加。
    """
    raw = _load_json(os.path.join(_BIAS_DIAG_DIR, "attribution.json"))
    if raw is None:
        return None

    # 类别键 → 责任侧：c1~c4 都是基准冻结/合成噪声侧，c5 是模型自身，c6 是叠加
    def _side_of(key: str) -> str:
        if key.startswith("c5"):
            return "model"
        if key.startswith("c6"):
            return "mixed"
        return "baseline"

    cats, rule = None, None
    if isinstance(raw, dict):
        rule = raw.get("rule")
        if raw.get("categories"):
            cats = raw["categories"]
        elif raw.get("summary"):
            cats = [
                {
                    "key": c["cat"],
                    "label": c["cat_label"],
                    "side": _side_of(c["cat"]),
                    "n": c["n"],
                    "desc": c.get("desc"),
                }
                for c in raw["summary"]
                if not str(c["cat"]).startswith("normal")
            ]
    elif isinstance(raw, list):
        agg = {}
        for r in raw:
            if not isinstance(r, dict) or r.get("cat") is None:
                continue
            a = agg.setdefault(
                r["cat"],
                {
                    "key": r["cat"],
                    "label": r.get("cat_label") or str(r["cat"]),
                    "side": _side_of(str(r["cat"])),
                    "n": 0,
                },
            )
            a["n"] += 1
        cats = sorted(agg.values(), key=lambda x: -x["n"])
    if not cats:
        return None

    total = sum(int(c.get("n") or 0) for c in cats)
    items = [
        {
            "key": c.get("key"),
            "label": c.get("label") or str(c.get("key")),
            "side": c.get("side") or "mixed",
            "desc": c.get("desc"),
            "n": int(c.get("n") or 0),
            "share_abnormal": round(int(c.get("n") or 0) / total, 4) if total else 0.0,
        }
        for c in cats
    ]
    baseline_n = sum(i["n"] for i in items if i["side"] == "baseline")
    model_n = sum(i["n"] for i in items if i["side"] == "model")
    return {
        "rule": rule,
        "items": items,
        "total": total,
        # 产物统计的异常笔数应与实时库一致；不一致说明产物过期，页面上直接提示
        "matches_live": n_abnormal is None or total == n_abnormal,
        "live_abnormal": n_abnormal,
        "baseline_n": baseline_n,
        "baseline_share": round(baseline_n / total, 4) if total else None,
        "model_n": model_n,
        "model_share": round(model_n / total, 4) if total else None,
    }


def _dropped_cities(report, seed):
    """找出「被整城剔除、模型只能吐全局回退价」的城市——东莞反例的主角。

    不靠人工点名，靠训练报告自证：cleaning.dropped_by_url 里有它（被清洗规则整城剔了
    样本），且 error_decomposition.by_city_mape 里没有它（测试集无本地样本 ⇒ 无本地价）。
    当前数据下这会自动选出 zs / zh / dg / yf 四城。
    """
    if not report or not seed:
        return []
    dropped = (report.get("cleaning") or {}).get("dropped_by_url") or {}
    scored = ((report.get("error_decomposition") or {}).get("by_city_mape")) or {}
    out = [
        {"city_code": code, "dropped_rows": int(dropped[code] or 0)}
        for code in seed["unit_price"]
        if code not in scored and code in dropped
    ]
    out.sort(key=lambda x: -x["dropped_rows"])
    return out


def _fallback_city_contrast(rows, report, seed):
    """东莞反例：模型侧几乎相同、基准侧差异巨大，异常率却从 0% 拉到 90%。

    模型侧用「AVM 估值 / 面积」的中位数当隐含单价（实时库现算），
    基准侧用 seed 冻结的 CITY_UNIT_PRICE（AST 现读），两边都不是写死的数。
    """
    if not seed:
        return None
    targets = _dropped_cities(report, seed)
    if not targets:
        return None

    by_code = {}
    for r in rows:
        if r["deviation_pct"] is None or not r["area"] or not r["avm_valuation"]:
            continue
        code = seed["name_to_code"].get(r["city"])
        if code is not None:
            by_code.setdefault(code, []).append(r)

    items = []
    for t in targets:
        rs = by_code.get(t["city_code"]) or []
        if not rs:
            continue
        implied = statistics.median([r["avm_valuation"] / r["area"] for r in rs])
        frozen = float(seed["unit_price"][t["city_code"]])
        abn = sum(1 for r in rs if r["abnormal_valuation"])
        items.append(
            {
                "city_code": t["city_code"],
                "city": seed["code_to_name"].get(t["city_code"], t["city_code"]),
                "dropped_rows": t["dropped_rows"],
                "n": len(rs),
                "model_unit_price": round(implied, 1),
                "frozen_unit_price": round(frozen, 1),
                "ratio": round(implied / frozen, 4) if frozen else None,
                "abnormal": abn,
                "abnormal_rate": round(abn / len(rs), 4),
            }
        )
    if not items:
        return None
    items.sort(key=lambda x: -x["abnormal_rate"])

    def _spread(key):
        vals = [i[key] for i in items]
        return round((max(vals) - min(vals)) / statistics.fmean(vals), 4)

    return {
        "items": items,
        # 极差 / 均值：模型侧接近 0 就说明「模型对这几个城市输出的是同一个数」
        "model_spread": _spread("model_unit_price"),
        "frozen_spread": _spread("frozen_unit_price"),
    }


def _baseline_self_check(seed):
    """冻结基准的内部一致性：把它当城市房价排名看，能不能自洽。

    只做一件不需要外部真值的事——按基准单价给城市排名。广东房价格局里深圳恒居首位，
    而冻结表把深圳排在广州之后，这本身就说明它不是市场价，而是某版模型的输出快照。
    """
    if not seed:
        return None
    ranked = sorted(seed["unit_price"].items(), key=lambda kv: -kv[1])
    gz, sz = seed["unit_price"].get("gz"), seed["unit_price"].get("sz")
    return {
        "top": [
            {
                "rank": i + 1,
                "city_code": code,
                "city": seed["code_to_name"].get(code, code),
                "unit_price": price,
            }
            for i, (code, price) in enumerate(ranked[:6])
        ],
        "sz_rank": next((i + 1 for i, (c, _p) in enumerate(ranked) if c == "sz"), None),
        "sz_over_gz": round(sz / gz, 4) if gz and sz else None,
    }


def _attribution_payload(rows, report):
    """组装归因区块所需数据；每一块独立降级，缺一块不影响其余块渲染。"""
    seed = _seed_constants()
    diag = _diag_rows()

    avm_rows = [r for r in rows if r["deviation_pct"] is not None]
    abnormal_cnt = sum(1 for r in avm_rows if r["abnormal_valuation"])

    payload = {
        "live": {
            "avm_rows": len(avm_rows),
            "abnormal": abnormal_cnt,
            "abnormal_rate": round(abnormal_cnt / len(avm_rows), 4) if avm_rows else 0.0,
            "threshold": DEVIATION_THRESHOLD,
            "model_version": (report or {}).get("version"),
        },
        "baseline": _baseline_self_check(seed),
        "city_contrast": _fallback_city_contrast(rows, report, seed),
        "attribution": _attribution_mix(abnormal_cnt),
        # 页面上逐块标注数据来自实时库还是离线产物——作品集里这点必须诚实
        "sources": {
            "live": "spacefin_crawler.dws_risk_class × spacefin.collateral（每次请求现算）",
            "diag": "output/avm/bias_diag/rows.json",
            "attribution": "output/avm/bias_diag/attribution.json",
            "seed": seed["source_ref"] if seed else None,
        },
        "provenance": (seed or {}).get("provenance"),
        "diag_available": bool(diag),
    }

    if not diag:
        return payload

    noise = [_E ** (-r["B"]) for r in diag]
    payload.update(
        {
            "diag_rows": len(diag),
            "variance": _variance_shares(diag),
            "counterfactual": _counterfactuals(diag),
            "noise_gradient": _noise_gradient(diag, (seed or {}).get("risk_classes")),
            # u 的实测区间，用来印证 B 确实就是 seed 里声明的那个 U(0.75,1.35)
            "noise_multiplier": {
                "min": round(min(noise), 4),
                "max": round(max(noise), 4),
                "declared": "U(0.75, 1.35)",
            },
        }
    )
    return payload


# ---------------- 路由 handler ----------------


def overview(ctx):
    """GET /api/avm —— 模型卡片 + 偏差分布 + 分维度异常率 + 版本血缘。"""
    report = _load_json(os.path.join(_AVM_OUT_DIR, "avm_report.json"))
    rows = _fetch_portfolio_rows()

    avm_rows = [r for r in rows if r["deviation_pct"] is not None]
    devs = [r["deviation_pct"] for r in avm_rows]
    signed = [r["signed_deviation_pct"] for r in avm_rows if r["signed_deviation_pct"] is not None]
    abnormal_cnt = sum(1 for r in avm_rows if r["abnormal_valuation"])

    counts = dict.fromkeys([b[0] for b in _DEV_BUCKETS], 0)
    for d in devs:
        counts[_pct_bucket(d)] += 1
    dev_hist = [
        {"bucket": label, "count": counts[label], "abnormal_zone": lo >= DEVIATION_THRESHOLD}
        for label, lo, _hi in _DEV_BUCKETS
    ]

    # 模型版本分组：同批跑出来的行版本一致才说明血缘干净；混版说明有行没重算。
    ver_agg = {}
    for r in rows:
        v = ver_agg.setdefault(r["model_version"], {"n": 0, "abnormal": 0})
        v["n"] += 1
        v["abnormal"] += 1 if r["abnormal_valuation"] else 0
    versions = [
        {"model_version": k, "n": v["n"], "abnormal": v["abnormal"]}
        for k, v in sorted(ver_agg.items(), key=lambda kv: -kv[1]["n"])
    ]

    tier_agg = {}
    for r in rows:
        tier_agg[r["source_tier"]] = tier_agg.get(r["source_tier"], 0) + 1
    tiers = [
        {"tier": k, "n": v, "pct": round(v / len(rows), 4) if rows else 0.0}
        for k, v in sorted(tier_agg.items(), key=lambda kv: -kv[1])
    ]

    return {
        "model": _model_card(report),
        "error_decomposition": _error_decomposition(report),
        "lineage": _lineage(),
        "portfolio": {
            "total": len(rows),
            "avm_rows": len(avm_rows),
            "avm_coverage": round(len(avm_rows) / len(rows), 4) if rows else 0.0,
            "abnormal": abnormal_cnt,
            "abnormal_rate": round(abnormal_cnt / len(avm_rows), 4) if avm_rows else 0.0,
            "threshold": DEVIATION_THRESHOLD,
            "median_abs_dev": round(statistics.median(devs), 4) if devs else None,
            "mean_abs_dev": round(statistics.fmean(devs), 4) if devs else None,
            "over_count": sum(1 for s in signed if s > 0),
            "under_count": sum(1 for s in signed if s <= 0),
            "median_signed_dev": round(statistics.median(signed), 4) if signed else None,
            "source_tiers": tiers,
            "versions": versions,
        },
        "dev_hist": dev_hist,
        "by_city": _dimension_stats(rows, lambda r: r["city"], "city"),
        "by_class": _dimension_stats(rows, lambda r: r["risk_class"], "risk_class"),
    }


def valuations(ctx):
    """GET /api/avm/valuations —— 估值列表：筛选（异常/偏差区间/版本/城市）+ 分页。

    筛选在 Python 侧做：城市维度来自另一个库（业务库地址），SQL 无法一次过滤；
    为保证「总条数」与筛选结果一致，索性所有条件统一在 join 之后应用。
    200 行量级下这是最简单且不会算错分页的做法。
    """
    q = ctx.query
    one = lambda k: (q.get(k) or [""])[0].strip()  # noqa: E731

    rows = _fetch_portfolio_rows()

    abnormal = one("abnormal")
    if abnormal in ("0", "1"):
        want = abnormal == "1"
        rows = [r for r in rows if r["abnormal_valuation"] == want]
    # 前端按「百分数」输入（30 表示 30%），这里换算回小数与 DWS 口径对齐。
    dev_min = _to_float(one("dev_min"))
    dev_max = _to_float(one("dev_max"))
    if dev_min is not None:
        rows = [r for r in rows if (r["deviation_pct"] or 0) >= dev_min / 100.0]
    if dev_max is not None:
        rows = [r for r in rows if (r["deviation_pct"] or 0) <= dev_max / 100.0]
    version = one("model_version")
    if version:
        rows = [r for r in rows if r["model_version"] == version]
    city = one("city")
    if city:
        rows = [r for r in rows if r["city"] == city]
    tier = one("source_tier")
    if tier:
        rows = [r for r in rows if r["source_tier"] == tier]

    # 默认按偏差降序：这是风控页，最不可信的估值必须排在第一屏。
    rows.sort(key=lambda r: (r["deviation_pct"] is None, -(r["deviation_pct"] or 0)))

    page = max(1, int(_to_float(one("page")) or 1))
    total = len(rows)
    start = (page - 1) * PAGE_SIZE
    return {
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
        "threshold": DEVIATION_THRESHOLD,
        "rows": rows[start : start + PAGE_SIZE],
    }


def abnormal_alerts(ctx):
    """GET /api/avm/alerts —— R-UNW-03 异常估值人工核查队列（ads_risk_valuation_alerts）。"""
    q = ctx.query
    page = max(1, int(_to_float((q.get("page") or ["1"])[0]) or 1))
    offset = (page - 1) * PAGE_SIZE

    crawl = db.crawl_conn()
    biz = db.biz_conn()
    try:
        cur = crawl.cursor()
        cur.execute("SELECT COUNT(*) FROM ads_risk_valuation_alerts")
        total = int(cur.fetchone()[0])
        cur.execute(
            "SELECT id, alert_code, loan_id, collateral_id, model_version, "
            "valuation_deviation_pct, detail, alert_date, etl_ts "
            "FROM ads_risk_valuation_alerts "
            "ORDER BY valuation_deviation_pct DESC, id ASC LIMIT %s OFFSET %s",
            (PAGE_SIZE, offset),
        )
        raw = cur.fetchall()

        # 按 alert_code 统计：当前只有 R-UNW-03，但 R-UBQ-01「不可溯源」也走这张表，
        # 分组展示才能在模型产物丢版本号时第一时间看出来。
        cur.execute(
            "SELECT alert_code, COUNT(*) FROM ads_risk_valuation_alerts GROUP BY alert_code"
        )
        by_code = [{"alert_code": r[0], "n": int(r[1])} for r in cur.fetchall()]
        cur.close()

        cids = [r[3] for r in raw if r[3] is not None]
        addrs = {}
        if cids:
            bcur = biz.cursor()
            placeholders = ",".join(["%s"] * len(cids))
            bcur.execute(
                f"SELECT collateral_id, property_addr FROM collateral "
                f"WHERE collateral_id IN ({placeholders})",
                cids,
            )
            addrs = {r[0]: r[1] for r in bcur.fetchall()}
            bcur.close()
    finally:
        crawl.close()
        biz.close()

    rows = [
        {
            "id": r[0],
            "alert_code": r[1],
            "loan_id": r[2],
            "collateral_id": r[3],
            "model_version": r[4],
            "deviation_pct": float(r[5]) if r[5] is not None else None,
            "detail": r[6],
            "alert_date": str(r[7]),
            "etl_ts": str(r[8]),
            "property_addr": addrs.get(r[3]) or "未标注",
            "city": _city_of(addrs.get(r[3])),
        }
        for r in raw
    ]
    return {
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
        "by_code": by_code,
        "rows": rows,
    }


def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def attribution(ctx):
    """GET /api/avm/attribution —— 38% 异常估值的归因区块。

    把 R-UNW-03 的异常率拆成「基准自指 / 模型自身 / seed 合成噪声」三分量，
    回答「这 38% 到底该记在谁头上」。结论：基准侧占异常 86.8%，真实模型误差 1 笔。
    数据来自离线诊断产物（output/avm/bias_diag/）与实时库；产物缺失时各块独立降级。
    """
    report = _load_json(os.path.join(_AVM_OUT_DIR, "avm_report.json"))
    rows = _fetch_portfolio_rows()
    return _attribution_payload(rows, report)


PAGE = {
    "id": "avm",
    "label": "估值精度看板",
    "roles": {"admin", "risk", "da"},
    "order": 70,
    "js": "p7_avm.js",
    "routes": {
        ("GET", "/api/avm"): overview,
        ("GET", "/api/avm/valuations"): valuations,
        ("GET", "/api/avm/alerts"): abnormal_alerts,
        ("GET", "/api/avm/attribution"): attribution,
    },
}
