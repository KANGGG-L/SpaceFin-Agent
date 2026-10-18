#!/usr/bin/env python3
"""房源市场生命周期状态机（S3 生命周期计划）。

上半部分是**纯函数层**（判定），下半部分是**调用层**（取证据 + 写 `dwd_listing_market_state`）。
判定逻辑只有 `decide_removal` 一份，调用层不许再写第二套 —— 影子期一旦出现两套口径，
「哪套是对的」就没人说得清了。

一句话：把「这套房源是不是下架了」压进一个不连库、不读 env 的纯函数，
让「我今天没扫到它」永远不会被算成「它下架了」。

用法：
  python tools/orchestrator/market_state.py --date 2026-08-31 --dry-run
  python tools/orchestrator/market_state.py --date 2026-08-31
退出码：0 = 计算并写入完成（含「当日全城不eligible」这种全 unknown 的结果）；
2 = 环境/工具失败（连不上库、缺表缺列），**仅强制模式**下才返回 2 ——
影子模式（`SPACEFIN_MARKET_STATE_ENFORCE=0`，默认）退出码恒 0，只记账不拦停 DAG。

**硬规则：缺省即未知。** 只有「当日被合格观测覆盖」的日子才有资格推进缺席计数；
观测不完整 / 历史遗留行 / 未扫到该页的日子，既不推进也不清零，一律记 unknown。
宁可那天少一个数据点，也不放一个假数据点 —— 指标量的是房产市场，不是我们的爬虫。

术语（已定稿，改本文件前先读懂）：
  active  在架  = 当日被**合格观测**覆盖且该房源出现 → streak 清零
  absent  消失  = 当日被合格观测覆盖但**未**出现 → `absent_streak += 1`（仅此条件）
  unknown 未知  = 当日观测不完整 / 历史遗留行 / 未扫到该页 → 不推进也**不清零**，
                  不参与任何计数。unknown 与 absent 必须严格区分，这是可信度的地基
  removed 下架  = `absent_streak >= N`（N 固定 3，D-5）且首次观测已满 `min_age_days`
                  （7 天，排除「刚上线还没被扫到」的假下架）

`eligible_today` 怎么算不归本文件管（它要读 `crawl_census` 的 `complete=1` 与页码区间，
同 `census.certified_pages` 口径）；本函数只消费它。**分支顺序是 `seen` 优先于 `eligible`**：

- 「今天看到了」是**直接证据**，不需要任何资格 —— `cutover`、页洞、账本缺失都拦不住它，
  一律 `active` 并清零 streak。若让 eligible 压在 seen 前面，已确认在架的房源会被报成
  unknown，在架数系统性少算，流转率的**分母**失真（分母失真比缺一个数据点更糟，
  因为它看起来是个合理的数）。
- 「今天没看到、能不能当作缺席证据」才需要资格：只有 `eligible=1` 时才推进 streak，
  否则记 unknown（不推进也不清零）。`eligible` 挡的是**缺席**，不是**在架**。

拿不准的输入（年龄未知、年龄为负、ds 早于首次观测）全部走保守分支：记 absent，
**不判 removed**。非法参数（N<1 / streak<0 / 日期格式错）是调用方 bug，抛 `ValueError`
响亮失败，绝不静默兜底成一个 0 或 1。

纯函数层不 import etl / census / db，不连库、不读环境变量、不做 IO ——
`pymysql` / `etl` / `census` 的 import 都延迟到调用层的函数里，
这样单测可以只喂 FakeConn 就跑完整条主流程。
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import time

# D-5 已定 N=3；7 天门槛见计划「产品视角 → 2. 指标定义」
DEFAULT_ABSENT_STREAK_N = 3
DEFAULT_MIN_AGE_DAYS = 7

STATUS_ACTIVE = "active"
STATUS_ABSENT = "absent"
STATUS_UNKNOWN = "unknown"
STATUS_REMOVED = "removed"

REASON_SEEN = "seen"
REASON_STREAK = "streak"
REASON_NOT_ELIGIBLE = "not_eligible"
REASON_CONFIRMED_ABSENT = "confirmed_absent"


def _as_date(value, field: str) -> datetime.date:
    """`datetime.date` / `datetime.datetime` / `'YYYY-MM-DD'` → `datetime.date`。

    其它类型一律 `ValueError`：日期算错会让整条 absent 链偏移一天，必须响亮失败。
    字符串只认补零的 `YYYY-MM-DD`（ds 是 DAG 的规范格式，`2026-8-31` 这种多半是
    拼接串日期时的 bug，宁可报错也不猜）；`strptime` 本身对个位数月份是宽松的，
    所以这里先做形状校验。
    """
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            return datetime.datetime.strptime(value.strip(), "%Y-%m-%d").date()
        raise ValueError(f"{field} 不是补零的 YYYY-MM-DD：{value!r}")
    raise ValueError(f"{field} 只接受 date/datetime 或 'YYYY-MM-DD' 字符串，收到 {value!r}")


def _require_int(value, field: str, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} 必须是 int，收到 {value!r}")
    if value < minimum:
        raise ValueError(f"{field} 必须 >= {minimum}，收到 {value!r}")
    return value


def decide_removal(
    seen_today,
    eligible_today,
    prev_streak,
    N,  # noqa: N803 — 大写 N 是计划文档锁定的签名，改名会让调用方与文档脱节
    first_seen_date,
    ds,
    min_age_days=7,
    prev_removed_date=None,
) -> tuple:
    """单日单房源的状态推进 → `(status, streak, removed_date, reason)`。

    参数：
      seen_today      当日是否在合格观测中出现
      eligible_today  当日该 (城,型) 是否具备合格观测；False 一律 unknown
      prev_streak     截至昨日的连续缺席天数（int >= 0）
      N               判下架所需的连续缺席天数（int >= 1，生产固定 3）
      first_seen_date 首次观测日；None = 年龄未知 → 永不判 removed
      ds              当日（date/datetime 或 'YYYY-MM-DD'）
      min_age_days    判下架所需的最小挂牌天数（int >= 0，默认 7）
      prev_removed_date 上一日已判定的下架日（可选）；传入即沿用，保证 removed_date 是
                       **首次**判定日。默认 None = 此前未下架，与旧行为完全一致。

    返回：
      status       `active|absent|unknown|removed`
      streak       推进后的连续缺席天数（unknown 日原样带回 `prev_streak`）
      removed_date **首次**判定下架的观测日（`datetime.date`）；未下架、
                   以及 active / absent 分支为 None
      reason       `seen|streak|not_eligible|confirmed_absent`

    `removed_date` 是首次判定日，不是「当日状态日」，且由纯函数自己持住 ——
    调用层**无需**再保留最早值。下架日期一旦逐日漂移或被抹成 NULL，
    流转率的时间序列就整条错位/出现空洞。三种去路：
      removed  `streak >= N` 后继续缺席 → 沿用 `prev_removed_date`（无则用当日）
      unknown  不合格观测日 → 原样带回 `prev_removed_date`（历史事实不因没看到而消失）
      active   重新在架 → None（下架状态解除）
    见 tests 中 `test_removed_date_stays_first_hit_day_as_streak_grows` 与
    `test_unknown_days_keep_removed_date_and_streak`。
    """
    _require_int(N, "N", 1)
    _require_int(prev_streak, "prev_streak", 0)
    _require_int(min_age_days, "min_age_days", 0)
    day = _as_date(ds, "ds")
    first_seen = None if first_seen_date is None else _as_date(first_seen_date, "first_seen_date")
    prior_removed = (
        None if prev_removed_date is None else _as_date(prev_removed_date, "prev_removed_date")
    )

    # seen 优先于 eligible：「今天看到了」是直接证据，不需要任何资格。
    # cutover / 页洞 / 账本缺失拦的是「缺席推断」，拦不住「在架事实」——
    # 若让 eligible 压在前面，已确认在架的房源会被报成 unknown，在架数系统性少算。
    if seen_today:
        return (STATUS_ACTIVE, 0, None, REASON_SEEN)

    if not eligible_today:
        # 没看到 + 没有缺席资格 = 缺省即未知：不推进（没扫全的日子不能当缺席证据），
        # 也不清零（不能擦掉已有证据）。已判定的下架日是历史事实，不因今天没看到而消失
        # —— 原样带回，否则调用层照抄写回会把下架日抹成 NULL，时间序列出现空洞。
        return (STATUS_UNKNOWN, prev_streak, prior_removed, REASON_NOT_ELIGIBLE)

    # 合格观测下未出现：唯一有资格推进 streak 的分支
    streak = prev_streak + 1
    if streak >= N and first_seen is not None:
        age_days = (day - first_seen).days  # 负数（首次观测在 ds 之后）自然被挡在门槛外
        if age_days >= min_age_days:
            # 沿用已有的下架日 = 首次判定日；首次判定时才落到当日
            return (
                STATUS_REMOVED,
                streak,
                prior_removed if prior_removed else day,
                REASON_CONFIRMED_ABSENT,
            )
    return (STATUS_ABSENT, streak, None, REASON_STREAK)


# ----------------------------------------------------------------------------
# 调用层：把纯函数接到库上（main / 读写 MySQL）
#
# 与纯函数的分工：上面只做判定，这里只做「取证据 + 写结果」。
# 判定逻辑**不许**在这里出现第二份 —— 一律走 decide_removal。
# ----------------------------------------------------------------------------

DWD_TABLES = (
    ("crawl_housing_sale", "sale"),
    ("crawl_housing_rent", "fangyuan"),
)
STATE_TABLE = "dwd_listing_market_state"

# eligible_reason 的取值（decision_reason 仍由 decide_removal 给，两者含义不同）
ELIGIBLE_OK = "ok"
E_NO_CERTIFIED_PAGES = "no_certified_pages"  # 该 (city,typ) 当日无 complete=1 —— 缺省即未知
E_NO_PAGE_EVIDENCE = "no_page_evidence"  # last_seen_page IS NULL（legacy 行）
E_PAGE_OUT_OF_RANGE = "page_out_of_range"  # 页码不在认证区间内
E_BEFORE_CUTOVER = "before_cutover"  # 首见日早于 cutover（历史遗留行）

# DWD.district 可能为 NULL（历史注释写的是「区/县」，实际存城市码）。city 列 NOT NULL，
# 且 'unknown' 匹配不上 crawl_census 的任何短码 → 自然落 eligible=0，正是保守方向。
UNKNOWN_CITY = "unknown"

# 本表 DDL 的**唯一源在这里**（写入方自带 DDL 并自己执行，照 census.py 的模式）：
# DAG 里的 market_state 任务会自己建表，而 `CREATE TABLE IF NOT EXISTS` 在活库上不补列，
# 所以 sql/crawl_schema.sql 必须与此逐列一致，否则先建表的一方会让另一方 INSERT 报
# Unknown column。改任何一列都要同步两处。
STATE_DDL = """
CREATE TABLE IF NOT EXISTS dwd_listing_market_state (
  ds               DATE          NOT NULL COMMENT '观测日',
  url_key          VARCHAR(64)   NOT NULL COMMENT '房源去重键（url_key.make_url_key）',
  city             VARCHAR(32)   NOT NULL COMMENT '城市归属，与 DWD.district 同口径：广东 21 城存短码，省外存子域原文',
  typ              VARCHAR(16)   NOT NULL COMMENT 'sale|fangyuan（采集任务类型口径）',
  market_status    VARCHAR(16)   NOT NULL COMMENT 'active|absent|unknown|removed',
  seen_today       TINYINT       NOT NULL DEFAULT 0 COMMENT '当日是否被观测到在架',
  eligible         TINYINT       NOT NULL DEFAULT 0 COMMENT '1=当日有资格作**缺席**证据（该 city-day complete=1 且页码在认证区间且首见日达标）。只看「能不能证明它没出现」，与 market_status 无关：active 且 eligible=0 是正常态（今天看到它在架，但它没有缺席资格），不是 bug',
  eligible_reason  VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '不合格原因：no_certified_pages / no_page_evidence / page_out_of_range / before_cutover',
  absent_streak    SMALLINT      NOT NULL DEFAULT 0 COMMENT '连续合格缺席天数；unknown 日不推进也不清零',
  last_seen_page   SMALLINT      NULL COMMENT '最近一次观测到在架时的列表页码',
  last_observed_date DATE        NULL COMMENT '最近一次观测到在架的观测日',
  first_seen_date  DATE          NULL COMMENT '首次观测日（取自 DWD，供 min_age_days 门槛重放/审计）',
  removed_date     DATE          NULL COMMENT 'decide_removal 确认下架的观测日（首次判定日，纯函数自持）',
  decision_reason  VARCHAR(32)   NOT NULL DEFAULT '' COMMENT 'seen|streak|not_eligible|confirmed_absent',
  created_at       TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (ds, url_key),
  KEY idx_ds_city_typ (ds, city, typ),
  KEY idx_ds_status (ds, market_status)
) ENGINE = InnoDB COMMENT = '房源市场状态派生表 · 每日逐 key 一行、可重放；缺席判定（absent）的唯一权威源'
"""

# 14 个业务列（不含 created_at），与 STATE_DDL 一一对应
_INSERT_SQL = f"""
INSERT INTO {STATE_TABLE} (
  ds, url_key, city, typ, market_status, seen_today, eligible, eligible_reason,
  absent_streak, last_seen_page, last_observed_date, first_seen_date, removed_date,
  decision_reason
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def log(msg: str) -> None:
    print(f"[market_state {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _env_flag(name: str, default: int = 0) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return bool(default)
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _certified_pages_fn():
    """延迟导入 `census.certified_pages`：认证页码的口径全仓库只有这一处。

    延迟是因为 census 会连带 import etl / url_city（生产侧口径），
    纯函数层与单测不该被它们拖累；import 失败必须冒泡，不做任何静默降级 ——
    拿不到账本 = 当日无认证页，那是**最保守**的结果，但必须让调用方看见报错。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from census import certified_pages

    return certified_pages


def _connect(dsn: str | None):
    """连 MySQL。延迟 import：纯函数层与单测不该被 pymysql / etl 拖累。"""
    import pymysql
    from etl import _mysql_params, load_env  # 复用生产侧口径，防第二套漂移（同 census.py）

    params = _mysql_params(dsn, load_env())
    return pymysql.connect(**params, autocommit=False, charset="utf8mb4")


def read_dwd_rows(conn, ds) -> list:
    """当日「被观测到在架」的行：`last_seen_date = ds`。

    rent 表 0 行是正常的（`TASK_TYPES=sale`），不报错、不告警到失败级别。
    `district` 存的是城市码（DWD 没有 city 列），别写成 city。
    """
    out = []
    for table, typ in DWD_TABLES:
        with conn.cursor() as cur:
            # last_seen_page 必须取：没有它就无法验证「这一页今天确实扫全了」
            cur.execute(
                f"SELECT url_key, district, first_seen_date, last_seen_page FROM {table} "
                "WHERE last_seen_date=%s",
                (ds,),
            )
            rows = cur.fetchall()
        for url_key, district, first_seen, page in rows:
            out.append(
                {
                    "url_key": url_key,
                    # district 实际存的是城市码（省外房源是子域原文），可能为 NULL
                    "city": district or UNKNOWN_CITY,
                    "typ": typ,
                    "first_seen_date": first_seen,
                    "last_seen_page": page,
                    "last_observed_date": ds,  # 今天被观测到在架
                }
            )
    return out


def read_census_pages(conn, ds) -> dict:
    """当日账本 → {(city,typ): 认证页码集合}。

    口径只有一处（`census.certified_pages`），这里不复制：只有 `complete=1` 的行
    才提供 `[first_page, contiguous_pages]`。没有 complete=1 的 (城,型) 不出现在
    返回值里 → 当日 eligible 全 0，这就是「缺省即未知」的落点。
    """
    certified_pages = _certified_pages_fn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT city, typ, complete, first_page, contiguous_pages "
            "FROM crawl_census WHERE ds=%s",
            (ds,),
        )
        rows = cur.fetchall()
    shaped = [
        {
            "city": c,
            "typ": t,
            "complete": int(complete or 0),
            "first_page": int(first or 0),
            "contiguous_pages": int(contig or 0),
        }
        for c, t, complete, first, contig in rows
    ]
    return certified_pages(shaped)


# city 必须装得下省外房源的子域原文（guangzhou / zhongshan 都超过 8 字符）；
# 窄列会静默截断成 guangzho，与 crawl_census 的短码 join 必然错位。
CITY_MIN_WIDTH = 32


def _insert_column_names() -> list:
    """INSERT 写的列名，从 `_INSERT_SQL` 派生 —— 不新增第三份手写定义。"""
    return [c.strip() for c in _INSERT_SQL.split("(", 2)[1].split(")")[0].split(",") if c.strip()]


def _ddl_column_defs() -> dict:
    """STATE_DDL 的 {列名: 列定义（类型+注释）}，只用于拼修复语句。"""
    out = {}
    body = STATE_DDL.split(f"{STATE_TABLE} (", 1)[1].split("\n)")[0]
    for line in body.splitlines():
        s = line.strip().rstrip(",")
        if not s or s.startswith(("PRIMARY KEY", "KEY", "UNIQUE")):
            continue
        name, _, rest = s.partition(" ")
        out[name] = rest.strip()
    return out


def check_state_columns(conn) -> list:
    """建表后校验：实际列 ⊇ INSERT 写的列，且 `city` 宽度足够。→ 问题清单（空 = 通过）。

    **`CREATE TABLE IF NOT EXISTS` 在活库上不补列**，所以「谁的 DDL 先建表、另一个就
    Unknown column」这个坑会反复出现（闸门 `last_seen_page`、DWD 三列、本表都踩过）。
    这里**只 fail loud、绝不自动 ALTER**：改线上表结构要由人看着报错决定；
    但必须同时给出现成的修复语句，否则报错等于把问题丢给下一个人。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, column_type, column_comment FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name=%s",
            (STATE_TABLE,),
        )
        rows = cur.fetchall()
    actual = {name: ((ctype or "").lower(), comment or "") for name, ctype, comment in rows}

    problems = []
    wanted = _insert_column_names()
    missing, no_comment = [], []
    for col in wanted:
        if col not in actual:
            missing.append(col)
        elif not actual[col][1].strip():
            # 注释是后来者判断「这列到底装什么」的唯一线索，漏带等于把线索抹掉
            no_comment.append(col)
    problems.extend(f"缺列 {c}" for c in missing)
    problems.extend(f"{c} 缺注释" for c in no_comment)

    m = re.search(r"varchar\((\d+)\)", actual.get("city", ("", ""))[0])
    narrow = not m or int(m.group(1)) < CITY_MIN_WIDTH
    if narrow:
        problems.append(
            f"city 宽度不足（现有 {actual.get('city', ('', ''))[0] or '缺列'}，"
            f"需 VARCHAR({CITY_MIN_WIDTH})）：省外子域原文会被静默截断，与账本短码 join 错位"
        )
    if not problems:
        return []

    log("ERROR 派生表结构与本文件不一致：" + "；".join(problems))
    print(f"  {STATE_TABLE} 需人工修复（本工具不自动改表结构）：")
    defs = _ddl_column_defs()
    for col in missing:
        print(f"    ALTER TABLE {STATE_TABLE} ADD COLUMN {col} {defs.get(col, '')};")
    for col in no_comment:
        print(f"    ALTER TABLE {STATE_TABLE} MODIFY COLUMN {col} {defs.get(col, '')};")
    if narrow:
        print(f"    ALTER TABLE {STATE_TABLE} MODIFY city VARCHAR({CITY_MIN_WIDTH}) NOT NULL;")
    return problems


def _ensure_state_table(conn) -> None:
    """建表要在**读**之前：首轮跑时派生表还不存在，`SELECT MAX(ds)` 会先炸。

    `CREATE TABLE IF NOT EXISTS` 幂等；写入方只有本文件，schema 文件由建表任务同步维护。
    """
    with conn.cursor() as cur:
        cur.execute(STATE_DDL)


def read_prev_state(conn, ds) -> tuple:
    """→ (prev_ds, {url_key: {...}})：**上一个已落库的观测日**的状态切片。

    取「ds 小于当日的最大 ds」而不是自然前一日：中间整天没跑时，
    前一天的状态仍然是最新的已知事实，中间的空白日等价于 unknown（不推进也不清零）。
    表不存在 / 无更早数据 → 返回 (None, {})，全部 key 按全新处理。
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT MAX(ds) FROM {STATE_TABLE} WHERE ds < %s", (ds,))
        row = cur.fetchone()
    prev_ds = row[0] if row else None
    if prev_ds is None:
        return None, {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT url_key, city, typ, market_status, absent_streak, removed_date, "
            "first_seen_date, last_seen_page, last_observed_date "
            f"FROM {STATE_TABLE} WHERE ds=%s",
            (prev_ds,),
        )
        rows = cur.fetchall()
    out = {}
    for key, city, typ, status, streak, removed, first_seen, page, last_obs in rows:
        out[key] = {
            "city": city,
            "typ": typ,
            "market_status": status,
            "absent_streak": int(streak or 0),
            "removed_date": removed,
            "first_seen_date": first_seen,
            "last_seen_page": page,
            "last_observed_date": last_obs,
        }
    return prev_ds, out


def judge_eligibility(city, typ, page, first_seen_date, certified: dict, cutover_date) -> tuple:
    """→ (eligible 0/1, reason)。三条件缺一即 0，顺序即下面判断的顺序。

    1. 该 (城,型) 当日有 `complete=1`（否则「今天没扫全」——没看 ≠ 没了）
    2. `last_seen_page` 落在认证页码区间内（NULL = legacy 行，无页码证据）
    3. `first_seen_date >= cutover_date`（cutover 前的行是历史遗留，永久判 unknown）
    """
    pages = certified.get((city, typ))
    if not pages:
        return 0, E_NO_CERTIFIED_PAGES
    if page is None:
        return 0, E_NO_PAGE_EVIDENCE
    if int(page) not in pages:
        return 0, E_PAGE_OUT_OF_RANGE
    if first_seen_date is None:
        return 0, E_NO_PAGE_EVIDENCE
    if _as_date(first_seen_date, "first_seen_date") < cutover_date:
        return 0, E_BEFORE_CUTOVER
    return 1, ELIGIBLE_OK


def project_day(
    *,
    dwd_rows: list,
    certified: dict,
    prev_state: dict,
    ds,
    cutover_date,
    n: int = DEFAULT_ABSENT_STREAK_N,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
) -> list:
    """单日全量投影 → 逐 key 一行（纯计算，不碰库，便于单测）。

    候选集 = **当日 DWD 里出现的 key** ∪ **上一日状态里的 key**：
    只看当日 DWD 的话，缺席的 key 今天根本没有行，永远不会被推进 ——
    下架就永远判不出来。缺席候选沿用上次已知的 `first_seen_date` / `last_seen_page` /
    `last_observed_date`，否则无从验证「它所在的那一页今天扫全了没有」。
    """
    universe: dict = {}
    for r in dwd_rows:
        universe[r["url_key"]] = {
            "city": r["city"],
            "typ": r["typ"],
            "seen_today": True,
            "first_seen_date": r["first_seen_date"],
            "last_seen_page": r["last_seen_page"],
            "last_observed_date": r.get("last_observed_date") or ds,
        }
    for key, p in prev_state.items():
        if key in universe:
            continue  # 今天出现了，以当日 DWD 的页码/首见日为准
        universe[key] = {
            "city": p["city"],
            "typ": p["typ"],
            "seen_today": False,
            "first_seen_date": p["first_seen_date"],
            "last_seen_page": p["last_seen_page"],
            "last_observed_date": p["last_observed_date"],  # 今天没看到 → 沿用上次
        }

    out = []
    for key in sorted(universe):
        r = universe[key]
        eligible, why = judge_eligibility(
            r["city"], r["typ"], r["last_seen_page"], r["first_seen_date"], certified, cutover_date
        )
        prev = prev_state.get(key, {})
        status, streak, removed_date, reason = decide_removal(
            r["seen_today"],
            eligible,
            int(prev.get("absent_streak") or 0),
            n,
            r["first_seen_date"],
            ds,
            min_age_days,
            prev.get("removed_date"),
        )
        out.append(
            {
                "ds": ds,
                "url_key": key,
                "city": r["city"],
                "typ": r["typ"],
                "market_status": status,
                "absent_streak": streak,
                "seen_today": 1 if r["seen_today"] else 0,
                "eligible": eligible,
                "eligible_reason": why,
                "decision_reason": reason,
                "removed_date": removed_date,
                "first_seen_date": r["first_seen_date"],
                "last_seen_page": r["last_seen_page"],
                "last_observed_date": r["last_observed_date"],
            }
        )
    return out


def write_state(conn, ds, rows: list) -> None:
    """当日整日重算：先 `DELETE WHERE ds=%s` 再全量写（与 census 的账本保持一致）。

    只 upsert 不清会留陈旧行 ——「昨天在架、今天没扫」的 key 若留在表里，
    重算时就读不到它，缺席链会断。
    """
    _ensure_state_table(conn)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {STATE_TABLE} WHERE ds=%s", (ds,))
        for r in rows:
            cur.execute(
                _INSERT_SQL,
                (
                    r["ds"],
                    r["url_key"],
                    r["city"],
                    r["typ"],
                    r["market_status"],
                    r["seen_today"],
                    r["eligible"],
                    r["eligible_reason"],
                    r["absent_streak"],
                    r["last_seen_page"],
                    r["last_observed_date"],
                    r["first_seen_date"],
                    r["removed_date"],
                    r["decision_reason"],
                ),
            )
    conn.commit()


def _report(ds, prev_ds, rows: list, certified: dict, enforce: bool, cutover_date) -> None:
    total = len(rows)
    stats = {s: 0 for s in (STATUS_ACTIVE, STATUS_ABSENT, STATUS_UNKNOWN, STATUS_REMOVED)}
    for r in rows:
        stats[r["market_status"]] += 1
    eligible = sum(1 for r in rows if r["eligible"])
    new_removed = sum(
        1 for r in rows if r["market_status"] == STATUS_REMOVED and r["removed_date"] == r["ds"]
    )
    print(f"=== {STATE_TABLE} ds={ds}（上一状态日 {prev_ds or '无'}，cutover {cutover_date}）===")
    print(
        f"  {total} 个 key："
        + " · ".join(
            f"{s}={stats[s]}"
            for s in (STATUS_ACTIVE, STATUS_ABSENT, STATUS_UNKNOWN, STATUS_REMOVED)
        )
    )
    print(
        f"  eligible={eligible}/{total}"
        + (f"（{round(100 * eligible / total)}%）" if total else "")
        + f"；认证 (城,型) {len(certified)} 个；今日新增 removed {new_removed}"
    )
    reasons = {}
    for r in rows:
        if not r["eligible"]:
            reasons[r["eligible_reason"]] = reasons.get(r["eligible_reason"], 0) + 1
    if reasons:
        print("  不合格原因：" + " · ".join(f"{k}={v}" for k, v in sorted(reasons.items())))
    print(f"  影子模式={'关（ENFORCE=1）' if enforce else '开（ENFORCE=0，只记账不拦停）'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", required=True, help="观测日 ds，YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="只算不写库")
    ap.add_argument(
        "--enforce",
        action="store_true",
        help="关闭影子模式（默认由 SPACEFIN_MARKET_STATE_ENFORCE 控制，0=影子）",
    )
    ap.add_argument(
        "--cutover-date",
        default=None,
        help="缺席资格的最早首见日；默认读 SPACEFIN_MARKET_STATE_CUTOVER，未设则取当日",
    )
    ap.add_argument("--mysql-dsn", default=None)
    args = ap.parse_args()

    enforce = args.enforce or _env_flag("SPACEFIN_MARKET_STATE_ENFORCE", 0)
    ds = _as_date(args.date, "--date")
    cutover_raw = args.cutover_date or os.getenv("SPACEFIN_MARKET_STATE_CUTOVER") or args.date
    cutover_date = _as_date(cutover_raw, "--cutover-date")

    try:
        conn = _connect(args.mysql_dsn)
        try:
            _ensure_state_table(conn)  # 首轮：先有表才读得到「上一日状态」
            if check_state_columns(conn):
                # 结构不对就不许往下算：算出来的结果也写不进去，不如当场报错
                return 2 if enforce else 0
            dwd_rows = read_dwd_rows(conn, ds)
            certified = read_census_pages(conn, ds)
            prev_ds, prev_state = read_prev_state(conn, ds)
            rows = project_day(
                dwd_rows=dwd_rows,
                certified=certified,
                prev_state=prev_state,
                ds=ds,
                cutover_date=cutover_date,
            )
            _report(ds, prev_ds, rows, certified, enforce, cutover_date)
            if not args.dry_run:
                write_state(conn, ds, rows)
                log(f"已写入 {len(rows)} 行（当日整日重算）")
            else:
                log("dry-run：未写库")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — 影子期不能因数据/环境问题拦停 DAG
        log(f"ERROR {type(exc).__name__}: {exc}")
        # 影子模式（默认）：只记账不拦停，退出码恒 0；强制模式才让失败冒泡成 2
        return 2 if enforce else 0
    log(f"完成 ds={ds}（enforce={1 if enforce else 0}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
