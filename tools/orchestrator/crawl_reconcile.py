#!/usr/bin/env python3
"""采集→各层行数对账（只读）。

用途：给定 ds，把「ETL 自称处理了什么」和「各层实际有多少行」放在一起对，
差异要么归零、要么必须被某条已知不变式解释，否则 exit 1。它是 S1/S2/S3 每阶段
的机器判据——人工读数只在它报差异时介入。

对账链：
  etl_processed.json 里该 ds 的文件集合（S1 后键为 `ds=<ds>/<fname>`；S1 前为 `<ds>/<fname>`
  指向 raw 顶层扁平文件，两代都认）
    → 行数 / 按 (city,type) 组内去重键数 / 全局去重键数
    → etl_report.json（raw_count / unique_total / inserted+updated / lake.rows）
    → MySQL crawl_housing_sale|rent（first_seen_date=ds / last_seen_date=ds）
    → data_lake/housing/dt=ds（parquet 行数）
    → [可选 --check-doris] Doris dwd.dwd_housing_*（全表行数 vs MySQL 全表行数）

已知不变式（用它解释差额，不是掩盖）：
  dedup() 按 (city,type) 分组去重，而 MySQL PK 与 write_lake 的全局 seen 集合
  按 url_key 全局去重，因此
      unique_total - 实际落库行数 == 跨城同键数（同一房源出现在多城 raw 里）
  差额被这条式子算平即为已解释。此时 upsert_dwd 的 inserted/updated 计数
  （按投喂行数而非受影响行数）会等量虚高。

WARN 档：内容级缺陷（省外库存、出租零落库），各层行数彼此并不矛盾，故不会被 FAIL
  抓到；它们不参与退出码，否则会淹没真正需要拦停的每轮回归。

用法：
  python tools/orchestrator/crawl_reconcile.py --date 2026-08-30
  python tools/orchestrator/crawl_reconcile.py --date 2026-08-30 --check-doris --json
退出码：0 = 全平或差额已被解释；1 = 存在无法解释的差异；2 = 用法/环境错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, ".."))

from etl import (  # noqa: E402  复用生产侧选择器与常量，避免第二套口径漂移
    _TABLE,
    CITY_NAMES,
    _load_state,
    _mysql_params,
    load_env,
)
from url_city import (  # noqa: E402  归属口径与 etl 同源，避免第二套判定
    R_URL_FOREIGN,
    R_URL_HIT,
    R_URL_HIT_OVER_FILENAME,
    resolve_city,
    subdomain_to_code,
)
from url_key import make_url_key  # noqa: E402

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"


def _log(msg: str) -> None:
    print(f"[reconcile {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def state_files_for(out_dir: str, date: str) -> list:
    """etl_processed.json 里记在该 ds 下的文件 → **相对 raw_dir 的路径**列表。

    两代状态键都要认，否则切换日之后的历史 ds 就对不了账：
    - S1 之后：`ds=<date>/<fname>` 就是真实相对路径（文件在 raw/ds=<date>/ 下）；
    - S1 之前：`<date>/<fname>` 的日期前缀是拼出来的，文件其实在 raw 顶层扁平存放。
    """
    state = _load_state(out_dir)
    out = []
    for k in state:
        if k.startswith(f"ds={date}/"):
            out.append(k)
        elif k.startswith(f"{date}/"):
            out.append(k[len(date) + 1 :])
    return sorted(out)


def scan_raw(raw_dir: str, rels: list) -> dict:
    """按 etl.load_rows 同样的分桶口径统计行去重键。

    `rels` 是相对 raw_dir 的路径（S1 后含 `ds=<date>/` 目录层）；**文件名**的城市/类型
    只能取 basename，与 load_rows 保持同一口径，否则 `ds=2026-09-02/gz` 会被当成城市码。
    但**分组键的 city 必须按 URL 子域重算**（方案 A）：etl 已按 resolve_city 分桶，这里若
    仍按文件名分桶，`group_distinct` 就不再等于 etl 的 unique_total，「组内去重」那条
    判据会假 FAIL。文件名城市码只用于「哪些文件该读」与 host_mismatch 统计。

    group_distinct = Σ 各 (解析后 city,type) 组内 distinct url_key = etl 的 unique_total
    global_distinct = 跨组 distinct url_key —— 等于 MySQL PK / lake 全局 seen 的实际行数
    host_*          = URL 子域归属（url_city.resolve_city，与 etl 同源）与文件名城市码的
                      一致性：CITY_NAMES 只闸文件名，从不闸内容
    """
    groups: dict = {}
    glob: set = set()
    foreign: set = set()
    mismatched: set = set()
    foreign_hosts: dict = {}
    lines = bad = skipped_files = host_unparsed = 0
    for rel in rels:
        parts = os.path.basename(rel).split("_")
        if len(parts) < 2:
            skipped_files += 1
            continue
        city, typ = parts[0], parts[1]
        if typ == "sale":
            typ_key = "sale"
        elif typ in ("fangyuan", "rent"):
            typ_key = "rent"
        else:
            skipped_files += 1
            continue
        if city not in CITY_NAMES:
            skipped_files += 1
            continue
        path = os.path.join(raw_dir, rel)
        if not os.path.exists(path):
            skipped_files += 1
            _log(f"WARN: 状态文件记了但磁盘已无此文件: {path}")
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    url = json.loads(line).get("url") or ""
                except json.JSONDecodeError:
                    bad += 1
                    continue
                k = make_url_key(url)
                glob.add(k)
                lines += 1
                # 归属口径与 etl 同源：anjuke 全拼子域 + 58 短码子域都认，
                # 否则 58 房源会被当成「不可解析」（实测 484 行，其中 fs/gz 264 行是真广东）
                city_eff, reason = resolve_city(url, city)
                groups.setdefault((city_eff, typ_key), set()).add(k)
                if reason in (R_URL_HIT, R_URL_HIT_OVER_FILENAME):
                    if city_eff != city:
                        mismatched.add(k)
                elif reason == R_URL_FOREIGN:
                    foreign.add(k)
                    foreign_hosts.setdefault(city_eff, set()).add(k)
                else:
                    host_unparsed += 1
    group_distinct = sum(len(v) for v in groups.values())
    top = sorted(foreign_hosts.items(), key=lambda kv: -len(kv[1]))[:10]
    return {
        "files": len(rels),
        "files_skipped_illegal": skipped_files,
        "lines": lines,
        "bad_json_lines": bad,
        "group_distinct": group_distinct,
        "global_distinct": len(glob),
        "cross_city_keys": group_distinct - len(glob),
        "host_foreign_keys": len(foreign),
        "host_mismatch_keys": len(mismatched),
        "host_unparsed_lines": host_unparsed,
        "foreign_host_cities": len(foreign_hosts),
        "foreign_host_top": [(h, len(ks)) for h, ks in top],
    }


def read_report(out_dir: str, date: str) -> dict:
    path = os.path.join(out_dir, "etl_report.json")
    if not os.path.exists(path):
        return {"missing": True}
    with open(path, encoding="utf-8") as f:
        rep = json.load(f)
    rep["missing"] = rep.get("date") != date
    return rep


def count_lake(lake_dir: str, date: str) -> dict:
    import pyarrow.parquet as pq

    root = os.path.join(lake_dir, f"dt={date}")
    out = {"exists": os.path.isdir(root), "rows": 0, "by_type": {}, "cities": 0}
    if not out["exists"]:
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        if not any(f.endswith(".parquet") for f in filenames):
            continue
        part_city = None
        for seg in dirpath.split(os.sep):
            if seg.startswith("city="):
                part_city = seg[5:]
        if part_city:
            out["cities"] += 1
        for fn in filenames:
            if not fn.endswith(".parquet"):
                continue
            n = pq.ParquetFile(os.path.join(dirpath, fn)).metadata.num_rows
            out["rows"] += n
            typ = "other"
            for seg in dirpath.split(os.sep):
                if seg.startswith("type="):
                    typ = seg[5:]
            out["by_type"][typ] = out["by_type"].get(typ, 0) + n
    return out


def query_mysql(params: dict, date: str, host_code: dict) -> dict:
    import pymysql

    conn = pymysql.connect(**params, autocommit=True, charset="utf8mb4")
    out = {}
    try:
        with conn.cursor() as cur:
            for typ, tbl in _TABLE.items():
                cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE first_seen_date=%s", (date,))
                first = cur.fetchone()[0]
                cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE last_seen_date=%s", (date,))
                last = cur.fetchone()[0]
                cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                total = cur.fetchone()[0]
                # 全表 URL 子域分布：省外库存占比是「按城算流转率」的分母污染，与当日切片未必同量
                cur.execute(
                    "SELECT SUBSTRING_INDEX(SUBSTRING_INDEX(SUBSTRING_INDEX(url,'//',-1),'/',1),'.',1) AS sub, "
                    f"COUNT(*) FROM {tbl} GROUP BY sub"
                )
                hosts = {str(r[0]): r[1] for r in cur.fetchall()}
                gd = sum(n for s, n in hosts.items() if s in host_code)
                out[typ] = {
                    "first_seen": first,
                    "last_seen": last,
                    "table_total": total,
                    "host_gd_rows": gd,
                    "host_foreign_rows": total - gd,
                    "host_foreign_cities": len([s for s in hosts if s not in host_code]),
                    "host_foreign_top": sorted(
                        ((s, n) for s, n in hosts.items() if s not in host_code),
                        key=lambda kv: -kv[1],
                    )[:10],
                }
    finally:
        conn.close()
    out["last_seen_total"] = sum(out[t]["last_seen"] for t in _TABLE)
    out["first_seen_total"] = sum(out[t]["first_seen"] for t in _TABLE)
    out["foreign_rows_total"] = sum(out[t]["host_foreign_rows"] for t in _TABLE)
    out["rows_total"] = sum(out[t]["table_total"] for t in _TABLE)
    return out


def query_doris() -> dict:
    """可选：Doris DWD 全表行数 vs MySQL 全表行数（lake_sync 是镜像非增量）。"""
    try:
        import pymysql

        lake_dir = os.path.join(_HERE, "..", "lake")
        sys.path.insert(0, os.path.abspath(lake_dir))
        import config  # tools/lake/config.py

        conn = pymysql.connect(
            host=config.DORIS["host"],
            port=config.DORIS["query_port"],
            user=config.DORIS["user"],
            password=config.DORIS["password"],
            database="dwd",
            autocommit=True,
        )
        out = {}
        try:
            with conn.cursor() as cur:
                for typ, tbl in (("sale", "dwd_housing_sale"), ("rent", "dwd_housing_rent")):
                    cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                    out[typ] = cur.fetchone()[0]
        finally:
            conn.close()
        return out
    except Exception as exc:  # noqa: BLE001 - 可选层，连不上就 SKIP 并说明原因
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", required=True, help="对账 ds，YYYY-MM-DD")
    ap.add_argument("--raw-dir", default="output/guangdong/raw")
    ap.add_argument("--out-dir", default="output/guangdong")
    ap.add_argument("--lake-dir", default="data_lake/housing")
    ap.add_argument("--mysql-dsn", default=None)
    ap.add_argument("--check-doris", action="store_true")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    date = args.date
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    # 全拼 ∪ 城市码：MySQL 侧按 host 第一级标签统计，58 的短码子域（fs/gz）也必须是广东
    host_code = subdomain_to_code()
    files = state_files_for(args.out_dir, date)
    if not files:
        _log(
            f"WARN: {args.out_dir}/etl_processed.json 里没有 {date}/ 或 ds={date}/ 前缀的键"
            f"（状态共 {len(_load_state(args.out_dir))} 条）"
        )
    raw = scan_raw(args.raw_dir, files)
    rep = read_report(args.out_dir, date)
    report_usable = isinstance(rep.get("input"), dict) and "lake" in rep and "incremental" in rep
    if rep.get("missing"):
        add(
            "etl_report 属于该 ds",
            SKIP,
            f"报告 date={rep.get('date')!r}，非 {date}。etl.py 只写单文件 output/guangdong/"
            f"etl_report.json（每次覆盖），故历史 ds 无法回溯对账",
        )
    elif not report_usable:
        add(
            "etl_report 可用",
            FAIL,
            f"{args.out_dir}/etl_report.json 缺失或缺 input/lake/incremental 段",
        )

    env = load_env()
    try:
        my = query_mysql(_mysql_params(args.mysql_dsn, env), date, host_code)
    except Exception as exc:  # noqa: BLE001 - 判据缺失必须显式失败而非静默
        my = None
        add("MySQL 可读", FAIL, f"连接/查询失败：{type(exc).__name__}: {exc}")
    lake = count_lake(args.lake_dir, date)
    doris = query_doris() if args.check_doris else None

    corpus_drift = ""
    drifted = False
    if not rep.get("missing") and report_usable:
        inp = rep["input"]
        drifted = inp["raw_count"] != raw["lines"]
        add(
            "raw 快照对该 ds 不可变（可对账）",
            PASS if not drifted else FAIL,
            f"报告 raw_count={inp['raw_count']} vs 现在实读 {raw['lines']}（差 {raw['lines'] - inp['raw_count']}）。"
            + (
                "实读==报告，该 ds 可逐层对账"
                if not drifted
                else "raw 是跨日追加的扁平文件，该 ds 已不可重放；"
                "下面两条混用「报告冻结值」与「现在实读」的判据因此只能降级为 WARN"
                "——S1 的 raw/ds= 目录就是修这个"
            ),
        )
        if drifted:
            corpus_drift = "（受上条 raw 行数漂移影响，非独立缺陷）"
        # 报告冻结值 vs 现在实读：漂移时不拦停，见上条判据
        add(
            "组内去重 == 报告 unique_total",
            PASS
            if (inp["unique_total"] == raw["group_distinct"] and not drifted)
            else (WARN if drifted else FAIL),
            f"报告 {inp['unique_total']} vs 实算 {raw['group_distinct']} {corpus_drift}",
        )
        fed = rep["incremental"]["total_inserted"] + rep["incremental"]["total_updated"]
        add(
            "投喂行数 == unique_total",
            PASS if fed == inp["unique_total"] else FAIL,
            f"inserted+updated={fed} vs unique_total={inp['unique_total']}（两者都只读报告，与语料漂移无关）",
        )
        add(
            "lake.rows 报告 == parquet 实数",
            PASS if rep["lake"]["rows"] == lake["rows"] else FAIL,
            f"报告 {rep['lake']['rows']} vs parquet {lake['rows']}（dt={date} 存在={lake['exists']}）",
        )
        missing_page = inp.get("rows_missing_page")
        if missing_page is not None:
            add(
                "页码证据覆盖率",
                PASS if missing_page == 0 else WARN,
                f"{missing_page}/{inp['unique_total']} 行 last_seen_page=NULL"
                f"（mode={rep.get('mode')}）"
                + (
                    "：这些行只证明 ETL 读到了它们，不证明今天在架；"
                    "G2 闸门的 refreshed 已把它们排除"
                    if missing_page
                    else ""
                ),
            )
        if my:
            gap_report = inp["unique_total"] - my["last_seen_total"]
            gap_keys = raw["cross_city_keys"]
            residual = gap_report - gap_keys
            add(
                "落库行数差额已被「跨城同键」解释",
                PASS if residual == 0 else (WARN if drifted else FAIL),
                f"unique_total - last_seen={gap_report} vs 跨城同键数 {gap_keys}"
                + ("" if residual == 0 else f"；未解释残差 {residual} {corpus_drift}"),
            )
            add(
                "inserted 与 first_seen 一致",
                PASS if rep["incremental"]["total_inserted"] == my["first_seen_total"] else FAIL,
                f"报告 inserted={rep['incremental']['total_inserted']} vs "
                f"DB first_seen_date={date} 行数={my['first_seen_total']}",
            )

    if my:
        add(
            "lake 行数 == MySQL 该 ds last_seen 行数",
            PASS if lake["rows"] == my["last_seen_total"] else FAIL,
            f"lake {lake['rows']} vs MySQL {my['last_seen_total']}",
        )
    if raw["host_foreign_keys"] or (my and my["foreign_rows_total"]):
        pct = round(100.0 * raw["host_foreign_keys"] / max(raw["global_distinct"], 1), 1)
        tbl_pct = round(100.0 * my["foreign_rows_total"] / max(my["rows_total"], 1), 1) if my else 0
        add(
            "URL 子域 ∈ 爬虫自己的 21 城白名单",
            WARN,
            f"本 ds 切片：{raw['host_foreign_keys']} 个键 / 全局去重 {raw['global_distinct']}"
            f"（{pct}%）的 anjuke 子域不在 21 城内，涉及 {raw['foreign_host_cities']} 个外来城市；"
            f"全表：{my['foreign_rows_total'] if my else '?'} 行 / {my['rows_total'] if my else '?'}"
            f"（{tbl_pct}%）。Top: "
            + ", ".join(f"{h}×{n}" for h, n in raw["foreign_host_top"])
            + f"；子域与文件名城市码不一致另有 {raw['host_mismatch_keys']} 键，"
            f"无法解析子域 {raw['host_unparsed_lines']} 行。"
            "CITY_NAMES 白名单只闸文件名从不闸内容，按城分组算流转率会把省外库存计入广东分母；"
            "两档比例不同，说明污染率逐日不稳",
        )
    if my and my["rent"]["table_total"] == 0:
        add(
            "出租链路有数据",
            WARN,
            f"crawl_housing_rent 全表 0 行、dt={date} 湖分区 by_type={lake['by_type']}。"
            "17 任务全绿但只有一半语料真的落库；生命周期指标目前只能对 sale 建",
        )
    if doris is not None:
        if "error" in doris:
            add("Doris DWD 行数", SKIP, doris["error"])
        elif my:
            for typ in ("sale", "rent"):
                add(
                    f"Doris dwd_housing_{typ} == MySQL 全表",
                    PASS if doris[typ] == my[typ]["table_total"] else FAIL,
                    f"Doris {doris[typ]} vs MySQL {my[typ]['table_total']}",
                )
    else:
        add("Doris DWD 行数", SKIP, "未加 --check-doris")

    bad = [c for c in checks if c["status"] == FAIL]
    payload = {"date": date, "raw": raw, "report": rep, "mysql": my, "lake": lake, "checks": checks}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"\n=== crawl_reconcile ds={date} ===")
        print(
            f"raw        文件 {raw['files']}（非法名跳过 {raw['files_skipped_illegal']}）"
            f" 行 {raw['lines']} 坏 JSON {raw['bad_json_lines']}"
        )
        print(
            f"           组内去重 {raw['group_distinct']}  全局去重 {raw['global_distinct']}"
            f"  跨城同键 {raw['cross_city_keys']}"
        )
        print(
            f"           省外子域键 {raw['host_foreign_keys']}（{raw['foreign_host_cities']} 城）"
            f"  子域≠文件名城市 {raw['host_mismatch_keys']}  子域不可解析 {raw['host_unparsed_lines']} 行"
        )
        if my:
            for typ in ("sale", "rent"):
                r = my[typ]
                print(
                    f"MySQL {typ:<5} first_seen={r['first_seen']:<7} last_seen={r['last_seen']:<7}"
                    f" 全表={r['table_total']} 省外子域={r['host_foreign_rows']}"
                    f"（{r['host_foreign_cities']} 城）"
                )
        print(
            f"lake       dt={date} 行数 {lake['rows']} 分区城市 {lake['cities']} by_type={lake['by_type']}"
        )
        if doris is not None and "error" not in doris:
            print(f"Doris      {doris}")
        print("--- 判据 ---")
        for c in checks:
            mark = {"PASS": "OK  ", "FAIL": "FAIL", "SKIP": "SKIP", "WARN": "WARN"}[c["status"]]
            print(f"[{mark}] {c['name']}: {c['detail']}")
        if raw["cross_city_keys"]:
            print(
                f"\n注：{raw['cross_city_keys']} 个 url_key 同时出现在多个 (city,type) 组。"
                f"落库/落湖按全局键先到先得，其 city 归属取决于文件名排序，不是业务判定。"
            )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
