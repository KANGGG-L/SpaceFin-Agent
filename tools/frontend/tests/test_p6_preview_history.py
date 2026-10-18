"""批次③-A · R2-7 推演历史落库的后端不变量（零 DB：db.write_audit 全部换成录制 stub）。

工单 §B R2-7 的四条硬边界在这里逐条锁住：
1. 不建新表，复用 ads_export_audit，action="policy_preview"，
   detail 只有 {hyp_text, rule, hit_loans, newly_hit_loans, hit_balance} 五个键；
2. _impact() 全站四个调用点中**只有 handle_preview 这一条路径**记审计
   ——handle_list（规则表逐条命中量）/ handle_suggest（草案逐条测算）/
   handle_save（已另有 policy_save）三处一行都不许多写；
3. 服务端历史端点只读、单次 ≤10 条；
4. 审计写失败绝不让用户的推演请求失败（detail 落库是留痕，不是推演的前置条件）。

外加只读端点不回退：GET /api/policy 的顶层载荷键不变（§1 批次③ 记录的同一条口径）。
"""

import json
import re

import db
import pages.p6_policy as p6
import pytest
from fakeconn import FakeConn

ZONE_ID = "price-gz-113.30-23.14"
# detail 里绝不能出现的样例 PII。路线 B 之后 detail 会落整份 impact 快照（逐笔
# loan_id / collateral_id / risk_class / addr / industry 均合规保留），唯一红线是
# 借款人标识：customer_id / customer_name 不得进审计库。
FORBIDDEN_IN_DETAIL = (
    "customer_id",
    "customer_name",
)


def _snapshot(n=3):
    """1 个高危网格 + n 笔全部命中（LTV > cap）的存量，地址带上真实格式供导出结论复核。"""
    zones = {
        ZONE_ID: {
            "zone_id": ZONE_ID,
            "zone_type": "price",
            "city": "gz",
            "center_lat": 23.14,
            "center_lng": 113.30,
            "sample_count": n,
            "median_unit_price": 30000.0,
            "median_ltv": 0.7,
            "price_dev_vs_city": -0.1,
            "is_high_risk_zone": 1,
            "high_risk_rule": "price_low",
        }
    }
    loans = [
        {
            "loan_id": i,
            "collateral_id": i,
            "balance": 1_000_000.0,
            "ltv": round(0.80 + 0.01 * i, 4),  # 全部 > cap 0.70 → 命中；部分 > 红线 0.85
            "risk_class": "关注",
            "low_confidence": 0,
            "is_high_risk_zone": 0,
            "lat": 23.14,
            "lng": 113.30,
            "addr": f"广州市黄埔区某路{i}号",
            "city": "gz",
        }
        for i in range(1, n + 1)
    ]
    # handle_list 会读 snapshot["zone_loans"]（网格命中索引，与 loans 同快照缓存）：
    # 缺这个键会 KeyError，让「只读路径没写审计」这条断言红在错误的原因上。
    return {"loans": loans, "zones": zones, "zone_loans": {ZONE_ID: list(range(n))}}


def _ctx(body=None, role="admin"):
    return type(
        "_Ctx",
        (),
        {
            "body": body or {},
            "ip": "10.0.0.9",
            "user": {"user": "r207tester", "role": role, "label": "系统管理员"},
        },
    )()


def _preview_body(**over):
    body = {
        "scope_type": "zone",
        "scope_value": ZONE_ID,
        "action": "reject",
        "ltv_cap": 0.70,
        "reason": "价格洼地网格",
    }
    body.update(over)
    return body


@pytest.fixture
def no_ddl(monkeypatch):
    """建表 DDL 不是本文件的被测对象，且它会真连 MySQL —— 一律换成 no-op（零 DB）。"""
    monkeypatch.setattr(p6, "ensure_policy_table", lambda: None)


@pytest.fixture
def audits(monkeypatch):
    """录制全部审计调用：(action, username, role, detail, result, ip)。"""
    calls = []
    monkeypatch.setattr(db, "write_audit", lambda *a, **k: calls.append(a))
    return calls


@pytest.fixture
def snap(monkeypatch, no_ddl):
    s = _snapshot()
    monkeypatch.setattr(p6, "_load_snapshot", lambda: s)
    return s


# ---------------- 1. handle_preview 写一条 policy_preview ----------------


def test_preview_writes_exactly_one_policy_preview_audit(snap, audits):
    out = p6.handle_preview(_ctx(_preview_body(hypothesis="广州核心区房价下跌 10%")))
    assert out["hit"]["loans"] == 3, "试算本身必须照常返回（审计是附带的）"
    assert len(audits) == 1, f"一次推演应只落一条审计，实际 {len(audits)} 条"
    action, username, role, detail, result, ip = audits[0]
    assert action == "policy_preview", "action 必须是 policy_preview（§B R2-7 第 1 条）"
    assert username == "r207tester" and role == "admin"
    assert result == "success"
    assert ip == "10.0.0.9", "审计要能追到来源，ip 由 RouteCtx 下发"
    assert isinstance(detail, str) and detail, "detail 必须是写入 ads_export_audit 的 JSON 文本"


def test_preview_price_up_is_not_live_shock(snap, audits):
    """回归守卫：preview 只对 price_drop 建**活 LTV 冲击**；上行情景（price_up）走
    /api/policy/stress 通道，不应在 preview 里偷偷产生「脱险」级活冲击。

    快照 ltv=0.81/0.82/0.83（≤ 红线 0.85）→ 基线 newly_hit=3；price_drop 冲击把全部
    推向越红线 → newly_hit=0；price_up 走 else→scenario_math（不建 shock）→ newly_hit
    应与基线一致（=3）。这钉死「preview 不接 price_up 活冲击」的文档边界，防未来误接。
    """
    base = p6.handle_preview(_ctx(_preview_body()))
    up = p6.handle_preview(_ctx(_preview_body(hypothesis="广州房价上涨 10%")))
    dn = p6.handle_preview(_ctx(_preview_body(hypothesis="广州房价下跌 10%")))

    # price_up 不建活冲击 → 命中面与无情景基线一致
    assert up["hit"]["loans"] == base["hit"]["loans"] == 3
    assert up["newly_hit"]["loans"] == base["newly_hit"]["loans"] == 3
    assert up["already_alert"]["loans"] == base["already_alert"]["loans"] == 0
    # 对照组：price_drop 确实产生活冲击（越红线面位移），证明断言不是恒真
    assert dn["newly_hit"]["loans"] == 0, "price_drop 冲击应把 ≤红线的笔全部推向越线（对照组）"
    assert dn["already_alert"]["loans"] == 3
    assert dn["newly_hit"]["loans"] != up["newly_hit"]["loans"]


def test_preview_audit_detail_keys_are_the_impact_snapshot(snap, audits):
    """路线 B：detail 是整份 impact 快照（含 rows 全量 + cover.rows 全量），
    顶层仍保留 hyp_text/rule/hit_loans/newly_hit_loans/hit_balance 兼容老读者。"""
    p6.handle_preview(_ctx(_preview_body(hypothesis="全国房价下跌 20%")))
    detail = json.loads(audits[0][3])
    assert "impact" in detail, "detail 必须含整份 impact 快照（路线 B）"
    assert detail["hyp_text"] == "全国房价下跌 20%"
    assert set(detail["rule"]) == {"scope_type", "scope_value", "action", "ltv_cap"}
    assert detail["rule"] == {
        "scope_type": "zone",
        "scope_value": ZONE_ID,
        "action": "reject",
        "ltv_cap": 0.7,
    }
    impact = detail["impact"]
    assert "rows" in impact and "cover" in impact, "快照缺 rows / cover"
    assert isinstance(impact["cover"], dict) and "rows" in impact["cover"]
    # 快照自愈力：无 impact 的老数据也能被 handle_history 解析（顶层 hit_loans 仍在）。
    # 该用例带「全国房价下跌 20%」情景（city=None 作用于任意作用域），冲击后 LTV 放大，
    # 3 笔全命中；其中 newly_hit 受冲击影响为 0（已越过红线变「已预警→提级处置」）。
    assert detail["hit_loans"] == 3
    assert detail["newly_hit_loans"] == 0
    assert detail["impact"]["hit"]["loans"] == 3


def test_preview_audit_numbers_match_the_same_impact_as_the_response(snap, audits):
    """「看着哪个数字按的保存」必须与响应里的数字同源：审计三数 == 同一次 _impact 的汇总。"""
    ctx = _ctx(_preview_body())
    out = p6.handle_preview(ctx)
    detail = json.loads(audits[0][3])
    assert (detail["hit_loans"], detail["newly_hit_loans"], detail["hit_balance"]) == (
        out["hit"]["loans"],
        out["newly_hit"]["loans"],
        out["hit"]["balance"],
    )
    # 这 3 笔 LTV 0.81/0.82/0.83 全部 > cap 0.70 且全部 ≤ 红线 0.85 → 全部是「新增受限」
    assert (detail["hit_loans"], detail["newly_hit_loans"], detail["hit_balance"]) == (
        3,
        3,
        3_000_000.0,
    )


def test_preview_audit_detail_carries_no_customer_pii(snap, audits):
    """PII 红线：审计快照不得含借款人标识（customer_id/customer_name）；
    抵押物地址（addr）与借款人行业（industry）**保留落库**——导出快照结论的
    逐笔明细要能对到具体押品地址与借款人行业（"该次推演受影响 N 笔"可复核验证）。"""
    p6.handle_preview(_ctx(_preview_body()))
    raw = audits[0][3]
    for token in FORBIDDEN_IN_DETAIL:
        assert token not in raw, f"detail 含 {token!r}（PII 不得进审计库）"
    # 逐笔 row 级断言：rows 与 cover.rows 都保留 addr/industry（供导出结论复核验证）。
    detail = json.loads(audits[0][3])
    for r in detail["impact"]["rows"]:
        assert "addr" in r and r["addr"], f"命中行缺 addr 键（导出结论需地址复核）：{r}"
        assert "industry" in r, f"命中行缺 industry 键（导出结论需行业复核）：{r}"
    for r in detail["impact"]["cover"]["rows"]:
        assert "addr" in r and r["addr"], f"覆盖行缺 addr 键（导出结论需地址复核）：{r}"
        assert "industry" in r, f"覆盖行缺 industry 键（导出结论需行业复核）：{r}"
    # 合规非 PII 字段应保留：快照确实带了逐笔明细（不是只存汇总数）。
    assert detail["impact"]["rows"][0]["loan_id"] is not None, "快照须含逐笔明细（loan_id）"
    # 数据一致性：cover.loans + hit.loans == in_scope.loans，余额同理。
    imp = detail["impact"]
    assert imp["cover"]["loans"] + imp["hit"]["loans"] == imp["in_scope"]["loans"], (
        "cover+hit≠in_scope"
    )
    assert (
        round(imp["cover"]["balance"] + imp["hit"]["balance"], 2) == imp["in_scope"]["balance"]
    ), "cover+hit 余额≠in_scope"


def test_preview_audit_detail_is_not_ascii_escaped(snap, audits):
    """ensure_ascii=False（与既有 policy_save 同一写法）：中文原样入库，审计页免读 \\uXXXX。"""
    p6.handle_preview(_ctx(_preview_body(hypothesis="房价下跌 10%")))
    raw = audits[0][3]
    assert "房价下跌 10%" in raw, f"detail 被转义了：{raw}"
    assert "\\u" not in raw


def test_non_admin_hypothesis_is_not_recorded_as_scenario(snap, audits):
    """非 admin 的 hypothesis 后端一律忽略 → 审计里也不能记成「规则 + 情景」。"""
    p6.handle_preview(_ctx(_preview_body(hypothesis="全国房价下跌 20%"), role="da"))
    detail = json.loads(audits[0][3])
    assert detail["hyp_text"] == "", "非 admin 的情景被当成已生效情景记录了下来"


# ---------------- 2. 另外三个 _impact() 调用点一行都不许多写 ----------------


def test_handle_list_writes_no_audit(snap, audits, monkeypatch):
    """护栏（改前即绿）：拦的是「只读列表路径多写审计」，不是实现缺位。"""
    monkeypatch.setattr(db, "crawl_conn", lambda: FakeConn())
    out = p6.handle_list(_ctx())
    assert audits == [], f"GET /api/policy 是只读路径，不得写任何审计：{audits}"
    assert {"policies", "union_impact", "zones", "cities", "actions", "can_write"} <= set(out)


def test_handle_suggest_writes_no_audit(snap, audits, monkeypatch):
    """护栏（改前即绿）：草案逐条测算这一处 _impact() 调用点不得写 policy_preview。"""
    monkeypatch.setattr(db, "crawl_conn", lambda: FakeConn())
    out = p6.handle_suggest(_ctx())
    assert out["total"] >= 1, "草案要真的算出来（本用例的前提是 _impact 已被调用）"
    assert audits == [], f"逐条测算属高频被动调用，不得写审计：{audits}"


def test_handle_save_writes_only_policy_save_not_preview(snap, audits, monkeypatch):
    """护栏（改前即绿）：保存路径已有 policy_save 留痕，不得再多一条 policy_preview。"""
    monkeypatch.setattr(db, "crawl_conn", lambda: FakeConn())
    p6.handle_save(_ctx(_preview_body()))
    actions = [a[0] for a in audits]
    assert actions == ["policy_save"], f"保存路径只能有 policy_save 一条，实际 {actions}"


def test_denied_save_does_not_write_preview(snap, audits, monkeypatch):
    """护栏（改前即绿）：越权拒绝仍走既有 policy_save/denied 留痕口径。"""
    monkeypatch.setattr(db, "crawl_conn", lambda: FakeConn())
    code, _ = p6.handle_save(_ctx(_preview_body(), role="da"))
    assert code == 403
    assert [(a[0], a[4]) for a in audits] == [("policy_save", "denied")], (
        "拒绝路径沿用既有 policy_save/denied 留痕，不得混入 policy_preview"
    )


# ---------------- 3. 审计写失败不影响用户推演 ----------------


def test_audit_write_failure_does_not_break_preview(snap, monkeypatch):
    """容错硬要求：审计写失败只能降级留痕，不能把用户的推演请求一起拖失败。

    先断言「确实尝试写过一次」，否则这条在改前（压根不写审计）也会绿 —— 那是假绿。
    """
    attempts = []

    def _boom(*a, **k):
        attempts.append(a)
        raise RuntimeError("MySQL 连接断开")

    monkeypatch.setattr(db, "write_audit", _boom)
    out = p6.handle_preview(_ctx(_preview_body()))
    assert len(attempts) == 1, f"推演路径压根没尝试写审计（尝试 {len(attempts)} 次）"
    assert out["hit"]["loans"] == 3, "审计写失败把推演请求拖失败了（§B R2-7 容错硬要求）"
    assert "process" in out and "scope_label" in out


def test_preview_still_raises_value_error_on_bad_input(snap, audits):
    """护栏（改前即绿）：参数非法仍按原样抛 ValueError 转 400，且不留一条成功审计。"""
    with pytest.raises(ValueError):
        p6.handle_preview(_ctx(_preview_body(scope_value="不存在的作用域")))
    assert audits == []


# ---------------- 4. 服务端历史端点：只读 + ≤10 条 ----------------

_AUDIT_COLS = ["id", "action", "username", "role", "detail", "result", "ip", "created_at"]


def _audit_rows(n):
    """n 行 policy_preview 审计（id 递增，最新在后；列顺序 = _AUDIT_COLS）。"""
    return [
        (
            100 + i,
            "policy_preview",
            "r207tester",
            "admin",
            json.dumps(
                {
                    "hyp_text": f"情景 {i}",
                    "rule": {
                        "scope_type": "zone",
                        "scope_value": ZONE_ID,
                        "action": "reject",
                        "ltv_cap": 0.7,
                    },
                    "hit_loans": i,
                    "newly_hit_loans": i,
                    "hit_balance": 1000.0 * i,
                },
                ensure_ascii=False,
            ),
            "success",
            "10.0.0.9",
            f"2026-08-30 10:0{i % 6}:00",
        )
        for i in range(1, n + 1)
    ]


class _LimitAwareCursor:
    """游标：录制 SQL + 按 SQL 里的 LIMIT 参数截断预置行。"""

    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(sql.split()), params))
        rows = list(self._conn.rows)
        # 真库先排序再截断；假库两步都得自己演，否则「最新在前」测的是夹具的写法。
        if re.search(r"ORDER BY\s+id\s+DESC", sql, re.I):
            rows.sort(key=lambda r: r[0], reverse=True)
        m = re.search(r"LIMIT\s+(-?\d+|\S+)", sql, re.I)
        if m:
            token = m.group(1)
            limit = int(token) if token.isdigit() else (params[-1] if params else None)
            if isinstance(limit, int):
                rows = rows[:limit]
        self._rows = rows
        return len(self._rows)

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _LimitAwareConn:
    """真库会执行 LIMIT，FakeConn 不会（预置几行就回几行）。

    「单次 ≤10 条」若直接拿 FakeConn 断言，测到的是假库的语义而不是 handler 的；
    这里补上 LIMIT 语义，判据就落在「handler 有没有真的把 10 传下去」，
    也免得为了让假库绿而在校验路径里再加一刀冗余的 Python 切片。
    """

    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def cursor(self):
        return _LimitAwareCursor(self)

    def close(self):
        pass

    def find_sql(self, *keywords):
        for sql, params in self.executed:
            if all(k in sql for k in keywords):
                return sql, params
        return None


def _history_conn(n):
    return _LimitAwareConn(_audit_rows(n))


def test_history_endpoint_exists_on_page_routes():
    assert ("GET", "/api/policy/history") in p6.PAGE["routes"], "未注册服务端历史端点"
    assert p6.PAGE["roles"] == {"admin"}, "页面角色不得因本项改动"


def test_history_endpoint_returns_at_most_ten_newest_first(monkeypatch, no_ddl):
    conn = _history_conn(14)
    monkeypatch.setattr(db, "crawl_conn", lambda: conn)
    out = p6.handle_history(_ctx())
    runs = out["runs"]
    assert len(runs) == 10, f"单次必须 ≤10 条，实际 {len(runs)}"
    ids = [r["id"] for r in runs]
    assert ids == sorted(ids, reverse=True), "历史要按时间倒序（最新在前）"
    assert runs[0]["rule"]["scope_value"] == ZONE_ID
    assert runs[0]["hit_loans"] is not None and "created_at" in runs[0]
    assert out["limit"] == 10 and out["read_only"] is True, (
        "响应要自述上限与只读性质：前端「这是历史记录」的措辞要靠它，不能凭猜"
    )


def test_history_endpoint_is_read_only(monkeypatch, no_ddl):
    conn = _history_conn(3)
    monkeypatch.setattr(db, "crawl_conn", lambda: conn)
    p6.handle_history(_ctx())
    # 词边界是必需的：裸子串扫 "CREATE" 会命中列名 CREATED_AT —— 那是假红（本用例第一版就红在这）。
    write_kw = re.compile(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE|TRUNCATE)\b", re.I)
    for sql, _params in conn.executed:
        assert not write_kw.search(sql), f"历史端点执行了写语句：{sql}"
        assert sql.strip().upper().startswith("SELECT"), f"历史端点只允许 SELECT：{sql}"
    sql, params = conn.find_sql("ads_export_audit", "LIMIT")
    assert sql is not None, "历史查询没有走 LIMIT（会把审计表整表拉回来）"
    assert params[-1] == p6.PREVIEW_HISTORY_LIMIT, f"LIMIT 未固定为 10：{params}"
    assert "policy_preview" in params, "历史查询未按 action 过滤"
    assert conn.executed and all("ads_export_audit" in s for s, _ in conn.executed)


def test_history_endpoint_survives_bad_detail_json(monkeypatch, no_ddl):
    """脏行只丢自己：审计表里可能留着旧版/别处写入的 detail，坏一条不能让端点 500，也不能连带丢掉好行。"""
    good = json.dumps(
        {
            "hyp_text": "好行",
            "rule": {
                "scope_type": "zone",
                "scope_value": ZONE_ID,
                "action": "reject",
                "ltv_cap": 0.7,
            },
            "hit_loans": 3,
            "newly_hit_loans": 1,
            "hit_balance": 1000.0,
        },
        ensure_ascii=False,
    )
    conn = FakeConn()
    conn.when(
        "FROM ads_export_audit",
        [
            (
                9,
                "policy_preview",
                "t",
                "admin",
                "{坏 JSON",
                "success",
                "10.0.0.9",
                "2026-08-30 10:00:00",
            ),
            (
                8,
                "policy_preview",
                "t",
                "admin",
                json.dumps({"hyp_text": "没有 rule"}),
                "success",
                "-",
                "2026-08-30 09:00:00",
            ),
            (7, "policy_preview", "t", "admin", good, "success", "-", "2026-08-30 08:00:00"),
        ],
        columns=_AUDIT_COLS,
    )
    monkeypatch.setattr(db, "crawl_conn", lambda: conn)
    out = p6.handle_history(_ctx())
    assert isinstance(out["runs"], list), "detail 脏数据不应让端点 500"
    assert [r["id"] for r in out["runs"]] == [7], (
        f"坏行要各自被跳过、好行必须留下：{[r['id'] for r in out['runs']]}"
    )


# ---------------- 5. shock 校验闭环：hypothesis 解析不出可算冲击 → 明示「按纯规则推演」 ----------------


def test_preview_unparseable_scenario_annotates_rule_only(snap, audits):
    """decompose/nl_parse 回告的 shock_text 只保证是原句子串，不保证可定量。用户确认后
    进 preview 时消费点复验：解析不出 → scenario_note 双轨 + scenario_rule_only 标注，
    前端据此在结论层明示降级，不让「界面挂着冲击、数字却是纯规则」静默发生。"""
    out = p6.handle_preview(_ctx(_preview_body(hypothesis="全国房价腰斩")))
    assert out["scenario_rule_only"] is True
    assert out["scenario_note_key"] == p6._SCENARIO_RULE_ONLY_NOTE
    assert out["scenario_note_params"] == {}
    assert out["scenario_note"] == out["scenario_note_key"], "zh-CN 原句兜底"
    # 定量侧不受影响：没有 shock 块、没有示意测算
    assert "shock" not in out
    assert "scenario_math" not in out


def test_preview_parseable_scenario_has_no_rule_only_flag(snap, audits):
    # 可解析情景（含利率/收入类示意测算）不携带降级标注
    for hyp in ("广州房价下跌 10%", "广州房价上涨 10%", "制造业收入下降 20%"):
        out = p6.handle_preview(_ctx(_preview_body(hypothesis=hyp)))
        assert "scenario_rule_only" not in out, f"{hyp} 可解析，不应标注纯规则降级"


def test_preview_nonadmin_hypothesis_ignored_no_annotation(snap, audits, no_ddl):
    # 非 admin 的 hypothesis 后端本就忽略 → 不存在「不可算降级」一说，不加标注
    out = p6.handle_preview(_ctx(_preview_body(hypothesis="全国房价腰斩"), role="da"))
    assert "scenario_rule_only" not in out
    assert "scenario_note" not in out
