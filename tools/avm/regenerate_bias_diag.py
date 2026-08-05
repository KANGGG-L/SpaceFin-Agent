#!/usr/bin/env python
"""重建 bias_diag/rows.json：用当前 canonical 模型的估值重新做 200 笔三分量分解。

背景：rows.json 原本是对 r4 模型做的逐行分解（A1 城市错位 / A2 模型内响应 /
B seed 噪声），当 canonical 模型更新（r11）后，估值 `est` 变了，分解必须重算，
否则页面归因区块与实时库（dws_risk_class 已是新模型的估值）口径不一致。

分解公式（与 bias_attribution.py 文件头一致，恒等式校验到机器精度）：
    u      = book / (CITY_UNIT_PRICE[city] * area)
    B      = -log(u)
    A      = log(est / (CITY_UNIT_PRICE[city] * area))
    A1     = 该城 A 的中位数            # 城市级系统性错位
    A2     = A - A1                     # 模型对面积/房龄/坐标的个体响应
    logdev = log(est / book) = A1 + A2 + B

跑完本脚本后再跑 `tools/avm/bias_attribution.py` 重建 attribution.json。
"""

import ast
import json
import os
import statistics
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "risk")
)

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DIAG = os.path.join(_REPO, "output", "avm", "bias_diag")
_SEED = os.path.join(_REPO, "seed", "generate_seed.py")


def _f(v):
    """MySQL DECIMAL 转 float，None 保持 None。"""
    return float(v) if v is not None else None


def seed_constants():
    with open(_SEED, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    got = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not node.targets:
            continue
        tgt = node.targets[0]
        if isinstance(tgt, ast.Name) and tgt.id in {"CITY_UNIT_PRICE", "CITIES"}:
            got[tgt.id] = ast.literal_eval(node.value)
    return got["CITY_UNIT_PRICE"], got["CITIES"]


def main():
    import pymysql

    unit_price, cities = seed_constants()
    # CITIES 是 [(名称, 城市码, 行政区, 包围盒), ...]，名称不带「市」后缀
    code_of = {f"{item[0]}市": item[1] for item in cities if len(item) >= 2}
    name_of = {item[1]: f"{item[0]}市" for item in cities if len(item) >= 2}

    env = {}
    with open(os.path.join(_REPO, ".env"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k] = v

    conn = pymysql.connect(
        host=env.get("MYSQL_HOST", "127.0.0.1"),
        port=int(env.get("MYSQL_PORT", "3306")),
        user="root",
        password=env.get("MYSQL_ROOT_PASSWORD", ""),
        database="spacefin_crawler",
        charset="utf8mb4",
    )
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.loan_id, r.collateral_id, r.market_valuation, r.ltv, r.risk_class,
               r.valuation_deviation_pct, r.abnormal_valuation,
               c.property_addr, c.area, c.true_market_price, c.lat, c.lng
        FROM dws_risk_class r JOIN spacefin.collateral c
          ON c.collateral_id = r.collateral_id
        """
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    cur.close()
    conn.close()

    out = []
    for r in rows:
        est = _f(r["market_valuation"])
        book = _f(r["true_market_price"])
        area = _f(r["area"])
        addr = (r["property_addr"] or "").strip()
        code = None
        for name, c in code_of.items():
            if addr.startswith(name):
                code = c
                break
        if code is None:
            code = "unknown"
        rec = {
            "loan_id": r["loan_id"],
            "cid": r["collateral_id"],
            "addr": addr,
            "city": name_of.get(code, code),
            "code": code,
            "area": area,
            "est": est,
            "book": book,
            "mv": est,
            "stored_dev": _f(r["valuation_deviation_pct"]),
            "abn": bool(r["abnormal_valuation"]),
            "ltv": _f(r["ltv"]),
            "balance": None,
            "age": None,
            "lat": _f(r["lat"]),
            "lng": _f(r["lng"]),
            "risk_class": r["risk_class"],
        }
        cup = unit_price.get(code)
        if est is not None and book and area and cup:
            # 分量命名沿用 bias_attribution.py 的公式符号（A1/A2/B/u），故用大写
            A = math_log(est / (cup * area))  # noqa: N806
            u = book / (cup * area)  # noqa: N806
            B = -math_log(u)  # noqa: N806
            rec.update({"A": A, "u": u, "B": B, "logdev": math_log(est / book)})
        out.append(rec)

    # A1 = 该城 A 的中位数；A2 = A - A1
    by_city: dict[str, list[float]] = {}
    for rec in out:
        if "A" in rec:
            by_city.setdefault(rec["code"], []).append(rec["A"])
    a1_of = {c: statistics.median(v) for c, v in by_city.items()}
    for rec in out:
        if "A" in rec:
            rec["A1"] = a1_of[rec["code"]]
            rec["A2"] = rec["A"] - rec["A1"]
            rec["signed"] = math_exp(rec["logdev"]) - 1.0

    os.makedirs(_DIAG, exist_ok=True)
    path = os.path.join(_DIAG, "rows.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    n_est = sum(1 for r in out if "A" in r)
    print(f"wrote {path}: {len(out)} rows, {n_est} with AVM est")


def math_log(x):
    import math

    return math.log(x)


def math_exp(x):
    import math

    return math.exp(x)


if __name__ == "__main__":
    main()
