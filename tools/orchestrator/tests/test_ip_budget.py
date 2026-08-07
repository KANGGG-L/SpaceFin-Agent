"""预算表解析（契约 §3.5）：默认值、总额 500/500、IP_BUDGET_JSON 覆盖、非法输入不抛异常。"""


def test_default_table_values_and_totals(load_master):
    m = load_master()
    t = m.IP_BUDGET
    assert len(t) == 21
    assert t["gz"] == {"sale": 60, "fangyuan": 60}
    assert t["sz"] == {"sale": 60, "fangyuan": 60}
    for c in ("zh", "st", "yf"):
        assert t[c] == {"sale": 20, "fangyuan": 20}
    assert sum(v["sale"] for v in t.values()) == 500
    assert sum(v["fangyuan"] for v in t.values()) == 500


def test_top_cities_and_per_type_env_override(load_master):
    m = load_master(
        BUDGET_TOP_CITIES="gz,dg",
        IP_BUDGET_SALE_TOP="50",
        IP_BUDGET_SALE_OTHER="10",
        IP_BUDGET_FY_TOP="30",
        IP_BUDGET_FY_OTHER="5",
    )
    t = m.IP_BUDGET
    assert t["gz"] == {"sale": 50, "fangyuan": 30}
    assert t["dg"] == {"sale": 50, "fangyuan": 30}
    assert t["sz"] == {"sale": 10, "fangyuan": 5}
    assert sum(v["sale"] for v in t.values()) == 50 * 2 + 10 * 19
    assert sum(v["fangyuan"] for v in t.values()) == 30 * 2 + 5 * 19


def test_ip_budget_json_override_applies(load_master):
    m = load_master(IP_BUDGET_JSON='{"gz": {"sale": 7, "fangyuan": 3}, "st": {"sale": 1}}')
    assert m.IP_BUDGET["gz"] == {"sale": 7, "fangyuan": 3}
    assert m.IP_BUDGET["st"] == {"sale": 1, "fangyuan": 20}  # 只覆盖给出的字段
    assert m.IP_BUDGET["sz"] == {"sale": 60, "fangyuan": 60}  # 未提及的城保持默认
    assert m._budget_of("gz", "sale") == 7


def test_ip_budget_json_invalid_is_ignored_not_raised(load_master):
    for bad in ("{not json", "[1,2,3]", '{"gz": 5}', '{"nocity": {"sale": 1}}', "   "):
        m = load_master(IP_BUDGET_JSON=bad)  # 不得抛异常
        assert m.IP_BUDGET["gz"] == {"sale": 60, "fangyuan": 60}
        assert sum(v["sale"] for v in m.IP_BUDGET.values()) == 500


def test_resolve_scope_whitelist(load_master):
    m = load_master()
    assert m._resolve_scope("gz", "sale") == ("gz", "sale")
    assert m._resolve_scope("gz", "fangyuan") == ("gz", "fangyuan")
    assert m._resolve_scope("", "") == (None, None)  # 缺省 → 不计预算
    assert m._resolve_scope("gz", "rent") == (None, None)  # rent 只在 ETL 侧，采集侧非法
    assert m._resolve_scope("beijing", "sale") == (None, None)
    assert m._resolve_scope("gz", "") == (None, None)


def test_budget_disabled_switch(load_master, rdb):
    m = load_master(IP_BUDGET_ENABLED="0")
    allowed, used, budget = m.try_consume_ip(rdb, "gz", "sale")
    assert allowed and used == 0 and budget == 0
    assert rdb.get(f"{m.IP_USED_PREFIX}gz:sale") is None
