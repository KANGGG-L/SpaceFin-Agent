"""i18n 静态守护（三合一 + 裸中文棘轮）。

i18n.js 头注释声称「静态测试会对 JS 源码里的中文字面量做正则断言」，本文件把该
承诺落成四个可执行的守门（此前只有 p6 局部有，p1/p3/p5/p7/p9 为零覆盖）：

1. 字典对齐：6 个页面命名空间 + app.js global + evidence _EVIDENCE_I18N 的
   en/zh-HK key 集合双向差集为空——单侧漏译会静默显示中文原文。
2. key↔调用点对账：页面 JS 里静态 t("…")/data-i18n 引用的 key 必须已在
   页面字典或 global 层注册（注释先剥除，避免把示例当调用点）。
3. 死 key：字典 key 必须出现在仓库语料（py/js/sql，sql 覆盖 DB 种子里
   t(r.city)/t(r.risk_class) 类动态命中）或拼装白名单。误删会让 en/zh-HK
   漏中文，所以宁进白名单（逐条注释）也不可错杀。
4. 裸中文棘轮：字典块之外的 CJK 行按文件冻结基线，只许减不许增——新增用户
   可见文案必须走 t()；数据键/常量映射类合法残留见基线注释。要新增合法残留
   时有意识地调低基线并注明原因。
5. key 漂移守门：后端下发（p6_policy/sandbox_agent/ai_decompose/p10_sandbox）
   的 *_key 字面量必须在前端字典注册，index.html 的 data-i18n* 与 app.js 自身
   的 t() 调用点也纳入对账；nl_parse 回告 notes 断言 en 译文无 CJK。

纯静态断言，不触网不落库（nl_parse notes 一条会惰性 import pages.p6_policy
做运行时取样，同样零 DB 零 HTTP）。
"""

import ast
import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent
STATIC = FRONTEND / "static"

PAGES = [
    ("pages/p1_datasource.js", "datasource"),
    ("pages/p3_migration.js", "migration"),
    ("pages/p5_spatial.js", "spatial"),
    ("pages/p6_policy.js", "policy"),
    ("pages/p7_avm.js", "avm"),
    ("pages/p9_compliance_audit.js", "compliance_audit"),
]

# 拼装型活 key 白名单：sandbox_agent.py 用字符串拼接构造 detail_key（基础句 +
# 条件后缀），纯文本对账会把它们误判为死 key。逐条注释拼装点。
ASSEMBLED_KEYS = {
    "未配置真实 LLM，叙事由确定性模板生成，以模拟对象事实为骨架（{profile}）——不参与定量结论",
    "模型 {model}（temperature 0.3）；按需调用风控工具获取量化依据后生成叙事与结论；模拟对象：{profile}",
    "模型 {model}（temperature 0.3）；按需调用风控工具获取量化依据后生成叙事与结论；评估对象：拟议规则「{rule}」；模拟对象：{profile}",
}

# 裸中文棘轮基线（字典块之外的 CJK 行数）。语义：
#   - p9/p6/p3/p5 的大头是 KIND_LABELS/RULE_LABEL/FEAT_ZH 等数据键映射与
#     LEGACY_PHRASES 词表——值经 t() 渲染，属合法残留；
#   - app.js 是 CITY_CODES/INDUSTRY_CODES/颜色映射/状态文案映射；
#   - p1/p7 已几乎清零（p1=5、p7=2）。
BARE_CJK_BASELINE = {
    "pages/p1_datasource.js": 5,
    "pages/p3_migration.js": 8,
    "pages/p5_spatial.js": 13,
    # 151：收入分支分组表 gLabelI 的 `g.key === "未标注"` 是后端数据值比较常量
    # （与房价分支 gLabel 同款），用户可见面已走 t("未标注") 翻译，不是漏翻文案。
    # 152：+1 为 decomposeInferenceCard 的 SUG_PREFIXES 常量行——「推断建议：/推斷建議：/
    # Inferred suggestion: 」是剥前缀用的匹配数据键（三语变体必须按字面写在源码里），
    # 不是用户可见文案；胶囊正文取自后端动态 why，展示面无新增裸中文。
    "pages/p6_policy.js": 152,
    "pages/p7_avm.js": 2,
    "pages/p9_compliance_audit.js": 80,
    "app.js": 96,
}

QKEY = re.compile(r'"((?:[^"\\]|\\.)*)"\s*:')
IKEY = re.compile(r'^\s*([^\s:\'"@/()][^:]*?)\s*:\s*"', re.M)
IKEY_LINE_ONLY = re.compile(r'^\s*([^\s:\'"@/()][^:]*?):\s*$')  # key 单独成行、value 在下一行


def _keys_of(seg):
    """解析字典块 key：带引号（可跨行 value）+ 简繁同形的不带引号标识符 key
    （含 key 行尾冒号、value 换行的排版）。"""
    out = set(QKEY.findall(seg))
    for line in seg.splitlines():
        m = IKEY.match(line) or IKEY_LINE_ONLY.match(line)
        if m:
            out.add(m.group(1).strip())
    return out


def _page_dict_keys(rel, ns):
    text = (STATIC / rel).read_text(encoding="utf-8")
    s1 = text.index('registerI18n("en"')
    e1 = re.search(r'\},\s*"' + ns + r'"\);', text[s1:]).end() + s1
    s2 = text.index('registerI18n("zh-HK"')
    e2 = re.search(r'\},\s*"' + ns + r'"\);', text[s2:]).end() + s2
    return _keys_of(text[s1:e1]), _keys_of(text[s2:e2]), text, (s1, e1, s2, e2)


def _global_keys():
    text = (STATIC / "app.js").read_text(encoding="utf-8")
    s1 = text.index('registerI18n("en"')
    s2 = text.index('registerI18n("zh-HK"')
    e2 = re.search(r"\n\}\);", text[s2:]).end() + s2
    return _keys_of(text[s1:s2]), _keys_of(text[s2:e2])


def _strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


def _corpus():
    parts = []
    for sub in ("tools", "sql"):
        base = FRONTEND.parent.parent / sub
        for p in sorted(base.rglob("*")):
            if p.suffix in (".py", ".js", ".sql") and p.is_file():
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def test_i18n_dict_parity_all_namespaces():
    """en/zh-HK 字典 key 集合逐命名空间对齐（页面 6 个 + app.js global）。"""
    g_en, g_hk = _global_keys()
    assert g_en == g_hk, "app.js global 字典 en/zh-HK 不对齐: " + str(sorted(g_en ^ g_hk))
    for rel, ns in PAGES:
        en, hk, _, _ = _page_dict_keys(rel, ns)
        assert en == hk, f"{rel} 字典 en/zh-HK 不对齐: {sorted(en ^ hk)}"


def _evidence_i18n():
    """解析 p10_sandbox.py 的 _EVIDENCE_I18N（evidence iframe 服务端三语字典）。"""
    src = (FRONTEND / "pages/p10_sandbox.py").read_text(encoding="utf-8")
    start = src.index("_EVIDENCE_I18N = ") + len("_EVIDENCE_I18N = ")
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
    return ast.literal_eval(src[start : i + 1])


def test_evidence_dict_parity():
    """p10 evidence 服务端三语字典（_EVIDENCE_I18N）三语 key 对齐。"""
    block = _evidence_i18n()
    langs = set(block)
    assert langs == {"zh-CN", "en", "zh-HK"}, f"evidence 字典语言集异常: {langs}"
    en, hk = set(block["en"]), set(block["zh-HK"])
    zh = set(block["zh-CN"])
    assert en == hk == zh, (
        f"evidence 字典三语 key 不对齐: en-zh={sorted(en ^ zh)}, en-hk={sorted(en ^ hk)}"
    )


def test_static_t_keys_registered():
    """每个页面 JS 静态 t("…")/data-i18n 引用的 key 必须已在页面字典或 global 层注册。"""
    g_en, _ = _global_keys()
    for rel, ns in PAGES:
        en, _, text, _ = _page_dict_keys(rel, ns)
        body = _strip_comments(text)
        static = set(re.findall(r'\bt\(\s*"((?:[^"\\]|\\.)*)"', body))
        static |= set(re.findall(r'data-i18n(?:-ph|-title|-aria)?="([^"]+)"', body))
        missing = sorted(k for k in static if k not in en and k not in g_en)
        assert not missing, f"{rel} 有 t()/data-i18n key 未注册: {missing}"


def test_no_dead_dict_keys():
    """字典 key 必须被仓库语料（py/js/sql）引用，或在拼装白名单内。"""
    corpus = _corpus()
    for rel, ns in PAGES:
        en, hk, text, (s1, e1, s2, e2) = _page_dict_keys(rel, ns)
        body = text[:s1] + text[e1:s2] + text[e2:]
        own_corpus = body + corpus
        dead = sorted(k for k in en if k not in own_corpus and k not in ASSEMBLED_KEYS)
        dead_hk = sorted(k for k in hk if k not in own_corpus and k not in ASSEMBLED_KEYS)
        assert not dead, f"{rel} en 字典死 key（误删会让 en/zh-HK 漏中文）: {dead}"
        assert not dead_hk, f"{rel} zh-HK 字典死 key: {dead_hk}"


@pytest.mark.parametrize("rel,ns", PAGES)
def test_bare_chinese_ratchet(rel, ns):
    """裸中文棘轮：字典块外 CJK 行数不得超过基线（只许减不许增）。"""
    _, _, text, (s1, e1, s2, e2) = _page_dict_keys(rel, ns)
    body = text[:s1] + text[e1:s2] + text[e2:]
    code = _strip_comments(body)
    code = re.sub(r"(?m)(?<![:\w])//[^\n]*$", "", code)  # 行尾注释
    code = re.sub(r'\bt\(\s*"(?:[^"\\]|\\.)*"\s*(?:,[^)]*)?\)', "", code)
    code = re.sub(r'data-i18n(?:-ph|-title|-aria)?="[^"]*"', "", code)
    n = sum(1 for line in code.splitlines() if re.search(r"[\u4e00-\u9fff]", line))
    baseline = BARE_CJK_BASELINE[rel]
    assert n <= baseline, (
        f"{rel} 裸中文行数 {n} 超过基线 {baseline}——新增用户可见文案必须走 t()；"
        "若为合法数据键/常量映射，请有意识地下调基线并注释原因"
    )


def test_bare_chinese_ratchet_app_js():
    text = (STATIC / "app.js").read_text(encoding="utf-8")
    s1 = text.index('registerI18n("en"')
    s2 = text.index('registerI18n("zh-HK"')
    e2 = re.search(r"\n\}\);", text[s2:]).end() + s2
    # 挖掉 en 块 [s1, s2) 与 zh-HK 块 [s2, e2)
    body = text[:s1] + text[e2:]
    code = _strip_comments(body)
    code = re.sub(r"(?m)(?<![:\w])//[^\n]*$", "", code)
    code = re.sub(r'\bt\(\s*"(?:[^"\\]|\\.)*"\s*(?:,[^)]*)?\)', "", code)
    code = re.sub(r'data-i18n(?:-ph|-title|-aria)?="[^"]*"', "", code)
    n = sum(1 for line in code.splitlines() if re.search(r"[\u4e00-\u9fff]", line))
    baseline = BARE_CJK_BASELINE["app.js"]
    assert n <= baseline, f"app.js 裸中文行数 {n} 超过基线 {baseline}"


# ---------------- key 漂移守门：后端下发 *_key ⊆ 前端字典 ----------------

# 后端文件 → 消费其下发 *_key 的前端字典（en 层；zh-HK 同集由对齐测试保证）。
# 消费点依据（static 源码里 grep 得到的 t() 调用）：
#   - p6_policy.py / sandbox_agent.py / ai_decompose.py 的 key 全部由 policy 页
#     渲染（推理链 notes/trace、合规 reason/impact/label、证据卡都在 p6_policy.js）；
#   - p10_sandbox.py 的 err_* 短 key 由服务端渲染的 evidence iframe 按
#     _EVIDENCE_I18N 查表；报告未生成的整句 error_key 由 p6_policy.js
#     t(data.error_key) 消费。
# 每个 registry 一并并上 app.js global（与 test_static_t_keys_registered 同口径：
# 公共词条上提后页面字典允许不再持有）、ASSEMBLED_KEYS（sandbox_agent 拼装
# detail_key）与 BACKEND_KEY_WHITELIST。
BACKEND_KEY_SOURCES = {
    "pages/p6_policy.py": ("pages/p6_policy.js",),
    "sandbox_agent.py": ("pages/p6_policy.js",),
    "ai_decompose.py": ("pages/p6_policy.js",),
    "pages/p10_sandbox.py": ("pages/p6_policy.js", "evidence"),
}

# 后端下发、但当前在前端没有 t() 消费点的 *_key（逐条注明去向）。这些 key 不
# 注册是对的：注册了反而成为字典死 key（前端源码语料里没有消费点）。
BACKEND_KEY_WHITELIST = {
    # _threshold_basis 阈值取值依据：中文原句由后端消费（P9 审计留痕 / CSV 导出），
    # p6_policy.js 不渲染 ltv_red_line_basis* / suggest_ltv_cap_basis*，暂无 t() 消费点。
    "与风险引擎同一条强预警线：LTV > {red} 就已进爆仓预警，本页不另立标准",
    "在红线 {red} 之下再压一档，压到「关注」级上界 {watch} 以下（五级分类 LTV 上界同源），故取 {cap}",
}

_KEY_FIELD = re.compile(r".+_key$")

# ai_decompose.py 的 key 有三类模板中转，AST 里不直接落在 *_key 字段上：
#   - _IMPACT_TPL/_REASON_TPL：{枚举: (zh, en, tw)}，zh（位 0）即 text_key；
#   - _RULE_ONLY_TEXT / _note(zh, en, tw)：zh 即 text_key；
#   - _map_label/_shock_template return 的模板串、_det_secondary 返回元组位 2
#     是 label_key 模板（其余位是枚举/数字，不能收）。
_AI_TPL_TABLES = {"_IMPACT_TPL", "_REASON_TPL", "_RULE_ONLY_TEXT"}
_AI_LABEL_FN_ZH_SLOT = {"_map_label": None, "_shock_template": None, "_det_secondary": 2}


class _BackendKeyScan:
    """「会进后端 *_key 字段的字符串字面量」收集器的取值解析。

    覆盖：值经局部变量中转（一阶名字解析）、+ 拼接做笛卡尔积、IfExp 取两支
    并集、*_keys.append(…) 后 sep.join 拼装——拼出的每个整句变体都是真实下发
    的 key，必须逐个注册。f-string / 动态下标无法静态定值，不收集：动态到这
    个程度的 key 走白名单或专门的行为测试（如 A6 拼装 key 由
    test_p6_process_chain_i18n.test_a6_mb_detail_key_registered_double_track 守）。
    """

    def __init__(self, tree):
        self.assigns = {}  # 变量名 -> [值表达式]（跨函数合并，一阶解析足够）
        self.appends = []  # (lineno, 列表名, 实参, 互斥组)——join 拼装要按源码顺序
        parent = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[id(child)] = node
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        self.assigns.setdefault(t.id, []).append(node.value)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
            ):
                self.appends.append(
                    (node.lineno, node.func.value.id, node.args, self._if_branch(node, parent))
                )
        self._memo = {}
        self._busy = set()

    @staticmethod
    def _if_branch(node, parent):
        """append 点所在的最近 if 的 id（用于互斥分组）。同一条 if 的 body/orelse
        两支互斥（if geo_district: 追加 A else: 追加 B 只会走其一），join 拼积时
        须并成一个「二选一」站点，否则会积出永不下发的假组合。"""
        cur = node
        while id(cur) in parent:
            par = parent[id(cur)]
            if isinstance(par, ast.If):
                if cur in par.body or cur in par.orelse:
                    return id(par)
                # 在 test 表达式内：不受该 if 分支约束，继续上溯
            cur = par
        return None

    def units(self, expr):
        """表达式可能取到的完整字符串集合（每个值都是一条潜在下发 key）。"""
        if expr is None:
            return set()
        if isinstance(expr, ast.Constant):
            return {expr.value} if isinstance(expr.value, str) else set()
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            return {a + b for a in self.units(expr.left) for b in self.units(expr.right)}
        if isinstance(expr, ast.IfExp):
            return self.units(expr.body) | self.units(expr.orelse)
        if isinstance(expr, ast.List):  # lines_key 这类「key 数组」：逐元素都是 key
            out = set()
            for el in expr.elts:
                out |= self.units(el)
            return out
        if isinstance(expr, ast.Name):
            return self._name_units(expr.id)
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Attribute)
            and expr.func.attr == "join"
            and isinstance(expr.func.value, ast.Constant)
            and isinstance(expr.func.value.value, str)
            and len(expr.args) == 1
            and isinstance(expr.args[0], ast.Name)
        ):
            return self._join_products(expr.args[0].id, expr.func.value.value)
        return set()

    def _name_units(self, name):
        if name in self._memo:
            return self._memo[name]
        if name in self._busy:  # 自引用防环
            return set()
        self._busy.add(name)
        out = set()
        for v in self.assigns.get(name, []):
            out |= self.units(v)
        self._busy.discard(name)
        self._memo[name] = out
        return out

    def _join_products(self, name, sep):
        """x_keys.append(…) 后 sep.join(x_keys)：每个 append 点按源码顺序可能参与
        也可能被条件跳过，笛卡尔积出全部整句变体（如 scope_note_key）。跳过的点
        不留分隔符（不积出「A；」这类半截串）；同一 if 的 body/orelse 两支互斥，
        先并成「二选一」站点再积。"""
        merged = {}  # 互斥组 id -> [lineno, 站点取值并集]
        plain = []  # 不在 if 分支里的独立 append 点
        for lineno, lst, args, group in self.appends:
            if lst != name:
                continue
            site = set()
            for a in args:
                site |= self.units(a)
            if group is None:
                plain.append((lineno, site))
            else:
                merged.setdefault(group, [lineno, set()])
                merged[group][1] |= site
        sites = sorted(plain + [(ln, s) for ln, s in merged.values()], key=lambda a: a[0])
        out = {""}
        for _, site in sites:
            nxt = set()
            for a in out:
                for b in site:
                    nxt.add(b if not a else (a if not b else a + sep + b))
            out |= nxt
        out.discard("")
        return out


def _backend_emitted_keys(path):
    """AST 扫描单个后端文件，收集所有会下发给前端的 *_key 字面量候选。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scan = _BackendKeyScan(tree)
    is_ai = path.name == "ai_decompose.py"
    is_p10 = path.name == "p10_sandbox.py"
    keys = set()

    def tpl_zh(value):
        # 三语 (zh, en, tw) 元组取 zh 位（zh 模板句即 key）
        if isinstance(value, ast.Tuple) and value.elts:
            return scan.units(value.elts[0])
        return set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            # ① {"xxx_key": 值}——值可为字面量/拼接/变量中转
            for k, v in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(k, ast.Constant)
                    and isinstance(k.value, str)
                    and _KEY_FIELD.match(k.value)
                ):
                    keys |= scan.units(v)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                # ② x["xxx_key"] = 值（如 sandbox_agent 的 trace detail_key 拼装）
                if (
                    isinstance(t, ast.Subscript)
                    and isinstance(t.slice, ast.Constant)
                    and isinstance(t.slice.value, str)
                    and _KEY_FIELD.match(t.slice.value)
                ):
                    keys |= scan.units(node.value)
                # ③ ai_decompose 模板表：zh 模板经表查位后流进 text_key/label_key
                if is_ai and isinstance(t, ast.Name) and t.id in _AI_TPL_TABLES:
                    v = node.value
                    if isinstance(v, ast.Dict):
                        for dv in v.values:
                            keys |= tpl_zh(dv)
                    else:
                        keys |= tpl_zh(v)
        elif isinstance(node, ast.Call):
            # ④ xxx_key="…" 关键字实参（如 fallback_reason_key）；api_key 是凭据不是 i18n key
            for kw in node.keywords:
                if kw.arg and kw.arg != "api_key" and _KEY_FIELD.match(kw.arg):
                    keys |= scan.units(kw.value)
            if is_ai and isinstance(node.func, ast.Name) and node.func.id == "_note" and node.args:
                # ⑤ _note(zh, en, tw)：zh 即 text_key
                keys |= scan.units(node.args[0])
            # ⑥ *_keys.append("模板句")：join 拼装的原料，单条命中时也原样下发
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and _KEY_FIELD.match(node.func.value.id)
            ):
                for a in node.args:
                    keys |= scan.units(a)
        elif isinstance(node, ast.FunctionDef):
            # ⑦ ai_decompose 标签模板函数：return 值（_det_secondary 取元组位 2）
            if is_ai and node.name in _AI_LABEL_FN_ZH_SLOT:
                slot = _AI_LABEL_FN_ZH_SLOT[node.name]
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Return) and sub.value is not None:
                        if slot is None:
                            keys |= scan.units(sub.value)
                        elif isinstance(sub.value, ast.Tuple) and len(sub.value.elts) > slot:
                            keys |= scan.units(sub.value.elts[slot])
        elif (
            is_p10
            and isinstance(node, ast.Tuple)
            and len(node.elts) == 3
            and isinstance(node.elts[2], ast.Dict)
        ):
            # ⑧ p10 错误三元组 (err_text, err_key, err_params)：位 1 是 _EVIDENCE_I18N 查表 key
            keys |= scan.units(node.elts[1])
    return keys


def test_backend_emitted_keys_registered():
    """后端下发给前端的 *_key 字面量必须已在对应前端字典（或白名单）注册。

    后端新发一个 key 而前端没注册时，en/zh-HK 会原样裸奔中文，这条先红。
    """
    g_en, _ = _global_keys()
    policy_en = _page_dict_keys("pages/p6_policy.js", "policy")[0]
    evidence_en = set(_evidence_i18n()["en"])
    registries = {
        "pages/p6_policy.js": policy_en | g_en | ASSEMBLED_KEYS | BACKEND_KEY_WHITELIST,
        "evidence": evidence_en | policy_en | g_en | ASSEMBLED_KEYS | BACKEND_KEY_WHITELIST,
    }
    problems = []
    for rel, sinks in BACKEND_KEY_SOURCES.items():
        reg = set()
        for s in sinks:
            reg |= registries[s]
        missing = sorted(k for k in _backend_emitted_keys(FRONTEND / rel) if k not in reg)
        if missing:
            problems.append(f"{rel} 下发但前端未注册的 *_key: {missing}")
    assert not problems, "\n".join(problems)


def test_index_html_data_i18n_registered():
    """index.html 的 data-i18n* key 必须已在 global 或任一页面字典注册。"""
    text = (STATIC / "index.html").read_text(encoding="utf-8")
    keys = set(re.findall(r'data-i18n(?:-ph|-title|-aria)?="([^"]+)"', text))
    g_en, _ = _global_keys()
    page_keys = set()
    for rel, ns in PAGES:
        page_keys |= _page_dict_keys(rel, ns)[0]
    missing = sorted(k for k in keys if k not in g_en and k not in page_keys)
    assert not missing, f"index.html 有 data-i18n key 未注册: {missing}"


def test_app_js_static_t_keys_registered():
    """app.js 自身静态 t("…")/data-i18n 引用的 key 必须已在 global 或页面字典注册。"""
    text = (STATIC / "app.js").read_text(encoding="utf-8")
    s1 = text.index('registerI18n("en"')
    s2 = text.index('registerI18n("zh-HK"')
    e2 = re.search(r"\n\}\);", text[s2:]).end() + s2
    body = _strip_comments(text[:s1] + text[e2:])
    static = set(re.findall(r'\bt\(\s*"((?:[^"\\]|\\.)*)"', body))
    static |= set(re.findall(r'data-i18n(?:-ph|-title|-aria)?="([^"]+)"', body))
    g_en, _ = _global_keys()
    page_keys = set()
    for rel, ns in PAGES:
        page_keys |= _page_dict_keys(rel, ns)[0]
    missing = sorted(k for k in static if k not in g_en and k not in page_keys)
    assert not missing, f"app.js 有 t()/data-i18n key 未注册: {missing}"


def _en_dict_value(rel, ns, key):
    """在页面 en 字典块里查 key 的 en 译文（带引号句 key 逐字查找；标识符型
    key 如 ltv_range_ignored 是不带引号注册的，补裸 key 形态）。"""
    text = (STATIC / rel).read_text(encoding="utf-8")
    s1 = text.index('registerI18n("en"')
    e1 = re.search(r'\},\s*"' + ns + r'"\);', text[s1:]).end() + s1
    seg = text[s1:e1]
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', seg)
    if not m and re.fullmatch(r"[A-Za-z_$][\w$]*", key):
        m = re.search(r"(?<![\w$])" + re.escape(key) + r"\s*:\s*" + r'"((?:[^"\\]|\\.)*)"', seg)
    return m.group(1) if m else None


def test_nl_parse_notes_i18n_en_clean():
    """_regex_parse_nl 回告 notes 双轨：notes_i18n 的 text_key 必须已注册，且 en
    译文不含 CJK——en 语言下「同时提到城市与行业…」「LTV 数值需在…」类回告
    消息不得裸奔中文。"""
    import pages.p6_policy as p6  # 惰性 import：conftest 已注入 sys.path，零 DB 零 HTTP

    # 两条已知 notes 分支都要真实命中（防测试空转）：
    # 城市+行业同现歧义提示 / LTV 越界未填入（500% 远超上限 200%）
    out_both = p6._regex_parse_nl("广州和制造业都要管，LTV 上限 500%")
    g_en, _ = _global_keys()
    policy_en = _page_dict_keys("pages/p6_policy.js", "policy")[0]
    keys = [n["text_key"] for n in out_both["notes_i18n"]]
    assert "同时提到城市与行业，已按城市填入；行业作用域请在下方表单改选" in keys, keys
    assert "ltv_range_ignored" in keys, keys
    for key in keys:
        assert key in policy_en or key in g_en, f"nl notes text_key 未注册: {key!r}"
        en = _en_dict_value("pages/p6_policy.js", "policy", key)
        assert en is not None, f"nl notes text_key 无 en 译文: {key!r}"
        assert not re.search(r"[\u4e00-\u9fff]", en), (
            f"nl notes text_key 的 en 译文含中文: {key!r} -> {en!r}"
        )
