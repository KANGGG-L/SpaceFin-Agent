#!/usr/bin/env python
"""P1 · 数据底座接入配置（设计评审 R-EVT-01 / 阶段2 L0，面向数据工程师）。

这一页回答两个问题：**接了哪些源（登记）、CDC 通不通**。

核心内容是 R-EVT-01 三项：数据源登记（业务库/日志/空间API）、CDC 开关、分类分级标签。
登记表是静态声明（我们说接了什么），CDC 状态是运行时事实（实际在不在流）——并排放才能发现
「登记里写着 CDC 开启，但 ODS 已经 40 分钟没有新事件」这种声明与现实的背离。

数据来源全部是真实运行态，无任何 mock：
  - 登记表 ads_datasource_registry（本页自建，root DDL + INSERT IGNORE 预置真实在用的源）
  - CDC 状态：ods_cdc_position（binlog 位点）/ ods_cdc_consumer_offset（消费水位）/
    ods_cdc_log（事件流水）

时区口径（踩过的坑，必须遵守）：宿主是 UTC、MySQL 容器是 +08:00。
所有「距今多久」一律用 SQL 侧 TIMESTAMPDIFF(..., NOW()) 计算，
绝不把 MySQL 返回的 naive datetime 拿到 Python 里和 datetime.now() 相减——那样恒差 8 小时。
"""

import json
import threading
from datetime import timedelta

# db 与本模块同属 tools/frontend，app.py 启动时已把该目录放进 sys.path。
import db

# ---------------- 登记表 ----------------

REGISTRY_TABLE = "ads_datasource_registry"

# 分类分级标签（R-UNW-02 合规要求）。顺序即敏感度递增，前端按此上色。
DATA_LEVELS = ["公开", "内部", "敏感", "PII"]
# 源类型：与设计评审 P1 的「业务库/日志/空间API」对齐，另加「爬虫」（本项目主力公开数据源）。
SOURCE_TYPES = ["业务库", "爬虫", "空间API", "日志"]

# 预置行 = 当前项目**真实在用**的数据源（对照 README「已接入组件」与运行中的容器/服务）。
# 用 INSERT IGNORE 灌入：重启不覆盖运维在页面上做过的开关调整。
# 登记面向评审展示：endpoint / service_unit 只写自然语言（接入方式、管控单元），
# 不出现连接串、host:port、容器/服务单元名等内部信息——那些属于密管与部署细节。
# 字段顺序：source_id, name, source_type, endpoint, cdc_enabled, data_level, owner, enabled,
#          target_table, service_unit, remark
PRESET_SOURCES = [
    (
        "biz_mysql",
        "信贷业务源库 spacefin",
        "业务库",
        "直连信贷业务主库，覆盖客户/押品/贷款三张业务表",
        1,
        "PII",
        "数据工程",
        1,
        None,
        "业务主库（Docker 部署）",
        "客户征信分/月收入/负债率属个人金融信息，仅供风险计算，导出须脱敏",
    ),
    (
        "binlog_cdc",
        "MySQL binlog 变更捕获",
        "日志",
        "订阅业务主库 binlog，实时捕获行级变更",
        1,
        "敏感",
        "数据工程",
        1,
        "ods_cdc_log",
        "CDC 实时采集服务",
        "变更前后的完整记录原样留存，内容等同源库，按源库同级管控",
    ),
    (
        "kafka_cdc_topic",
        "Kafka 事件总线 spacefin.cdc.log",
        "日志",
        "CDC 事件经 Kafka 事件总线对外分发",
        1,
        "敏感",
        "数据工程",
        1,
        "ods_cdc_consumer_offset",
        "流式事件生产服务",
        "消费进度实时对账，至少一次送达，事件不丢失",
    ),
    (
        "anjuke_sale",
        "安居客二手房挂牌采集（广东 21 城）",
        "爬虫",
        "分布式批量采集：调度中心 + Redis 队列 + 代理池",
        0,
        "公开",
        "数据工程",
        1,
        "crawl_housing_sale",
        "每日 00:30 定时采集调度",
        "公开挂牌信息，同一房源跨日自动去重；批量采集不适用实时链路",
    ),
    (
        "anjuke_rent",
        "安居客租房挂牌采集",
        "爬虫",
        "宿主浏览器渲染采集，经代理池出网",
        0,
        "公开",
        "数据工程",
        1,
        "crawl_housing_rent",
        "宿主浏览器渲染服务",
        "页面需真实浏览器渲染；容器内渲染已被对方反爬识别，改走宿主",
    ),
    (
        "tencent_geocode",
        "腾讯位置服务 · 地理编码",
        "空间API",
        "腾讯位置服务地理编码开放接口",
        0,
        "内部",
        "算法/数据工程",
        1,
        "community_coords",
        "ETL 收尾异步回填任务",
        "调用配额有限，结果本地落库复用，避免重复消耗配额",
    ),
]


# 建表 + 预置只需成功一次；用标志位避免每个请求都发 DDL。
# 加锁是因为 app.py 用的是 ThreadingHTTPServer，多请求会并发进这里。
_ready = False
_ready_lock = threading.Lock()


def _ensure_registry():
    """幂等建登记表并预置真实数据源。

    为什么在请求里懒建而不是模块导入时建：pages/__init__ 在服务启动时 import 本模块，
    此刻若 MySQL 未就绪会被记进 LOAD_ERRORS，整页直接消失。放到首个请求里建，
    DB 抖动最多让这一次请求报错，恢复后自愈。
    """
    global _ready
    if _ready:
        return
    with _ready_lock:
        if _ready:
            return
        conn = (
            db.ddl_conn()
        )  # app 用户无 DDL 权限，建表必须走 root（同 db.ensure_alert_confirm_table）
        try:
            cur = conn.cursor()
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {REGISTRY_TABLE} ("
                "source_id VARCHAR(32) PRIMARY KEY,"
                "name VARCHAR(64) NOT NULL,"
                "source_type VARCHAR(16) NOT NULL,"
                "endpoint VARCHAR(255) NOT NULL,"
                "cdc_enabled TINYINT NOT NULL DEFAULT 0,"
                "data_level VARCHAR(8) NOT NULL DEFAULT '内部',"
                "owner VARCHAR(32) NOT NULL,"
                "enabled TINYINT NOT NULL DEFAULT 1,"
                "target_table VARCHAR(64) NULL,"
                "service_unit VARCHAR(64) NULL,"
                "remark VARCHAR(255) NULL,"
                "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
                "KEY idx_level (data_level)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            # INSERT IGNORE：主键冲突即跳过，不覆盖页面上改过的 cdc_enabled/enabled。
            cur.executemany(
                f"INSERT IGNORE INTO {REGISTRY_TABLE} "
                "(source_id,name,source_type,endpoint,cdc_enabled,data_level,owner,enabled,"
                "target_table,service_unit,remark) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                PRESET_SOURCES,
            )
            # 登记文案校正：endpoint/service_unit/remark 的口径在代码侧修订过（如去内部
            # 信息、改自然语言）时，同步到已入库的旧行——INSERT IGNORE 不会碰已存在行，
            # 而这三个字段页面不可改，代码即权威口径（cdc_enabled/enabled 仍以页面为准）。
            cur.executemany(
                f"UPDATE {REGISTRY_TABLE} SET endpoint=%s, service_unit=%s, remark=%s "
                "WHERE source_id=%s AND (endpoint<>%s OR NOT (service_unit<=>%s) "
                "OR NOT (remark<=>%s))",
                [(s[3], s[9], s[10], s[0], s[3], s[9], s[10]) for s in PRESET_SOURCES],
            )
            conn.commit()
            cur.close()
            _ready = True
        finally:
            conn.close()


CONSUMERS = ["risk_downstream", "kafka_stream_producer"]


def _cdc_state(crawl_cur):
    """CDC 位点 / 消费水位 / 滞后量。返回值直接喂给前端 CDC 卡片。"""
    state = {"position": None, "master": None, "consumers": [], "max_event_id": 0}

    crawl_cur.execute("SELECT COALESCE(MAX(id),0) FROM ods_cdc_log")
    max_id = int(crawl_cur.fetchone()[0])
    state["max_event_id"] = max_id

    crawl_cur.execute(
        "SELECT log_file, log_pos, updated_at, TIMESTAMPDIFF(SECOND, updated_at, NOW()) "
        "FROM ods_cdc_position WHERE repl_key='binlog'"
    )
    row = crawl_cur.fetchone()
    if row:
        state["position"] = {
            "log_file": row[0],
            "log_pos": int(row[1]),
            "updated_at": str(row[2]),
            "stale_seconds": (int(row[3]) if row[3] is not None else None),
        }

    # 消费水位：lag 用 id 差而非时间差——同秒多事件下时间戳会漏算（与 consumer.py 同口径）。
    crawl_cur.execute(
        "SELECT consumer, last_id, updated_at, TIMESTAMPDIFF(SECOND, updated_at, NOW()) "
        "FROM ods_cdc_consumer_offset ORDER BY consumer"
    )
    known = {}
    for r in crawl_cur.fetchall():
        known[r[0]] = {
            "consumer": r[0],
            "last_id": int(r[1]),
            "updated_at": str(r[2]),
            "idle_seconds": (int(r[3]) if r[3] is not None else None),
            "lag": max(0, max_id - int(r[1])),
        }
    # 按固定顺序输出已知消费者，未登记过水位的补一行占位，避免页面上「少了一个消费者」看不出来。
    for name in CONSUMERS:
        state["consumers"].append(
            known.pop(
                name,
                {
                    "consumer": name,
                    "last_id": 0,
                    "updated_at": None,
                    "idle_seconds": None,
                    "lag": max_id,
                },
            )
        )
    state["consumers"].extend(known.values())
    return state


def _source_probe():
    """从业务源库侧取两个佐证：主库 binlog 位点 + 业务表最新一行的年龄（秒）。

    两者都必须走 root（db.biz_conn）：app 用户既没有 REPLICATION CLIENT 权限，
    也读不到 spacefin 库。任一步失败都返回 None，判定自动降级，不让整页 500。

    为什么要「业务表最新行年龄」：主库位点是**弱信号**（见 get_datasource 里的说明），
    而「业务库出现了比 ODS 最后一条事件还新的行」是**强信号**——那是实打实的漏采。
    这里只用 created_at（覆盖 INSERT），UPDATE 覆盖不到，所以它能证伪不能证明。
    年龄同样在 SQL 侧算，与 ODS 侧的 age 都以同一台 MySQL 的 NOW() 为基准，可直接比大小。
    """
    out = {"master": None, "biz_newest_age": None}
    conn = db.biz_conn()
    try:
        cur = conn.cursor()
        try:
            cur.execute("SHOW MASTER STATUS")
            row = cur.fetchone()
            if row:
                out["master"] = {"log_file": row[0], "log_pos": int(row[1])}
        except Exception:  # noqa: BLE001 —— 权限/MySQL 版本差异
            pass
        try:
            cur.execute(
                "SELECT MIN(age) FROM ("
                "SELECT TIMESTAMPDIFF(SECOND, MAX(created_at), NOW()) age FROM loan "
                "UNION ALL "
                "SELECT TIMESTAMPDIFF(SECOND, MAX(created_at), NOW()) FROM customer"
                ") t"
            )
            row = cur.fetchone()
            out["biz_newest_age"] = int(row[0]) if row and row[0] is not None else None
        except Exception:  # noqa: BLE001
            pass
        cur.close()
        return out
    finally:
        conn.close()


def _pos_ahead(a, b):
    """(a) 是否严格领先 (b)。binlog 文件名形如 binlog.000002，字典序即时间序（同 tools/cdc/main.py）。"""
    if not a or not b:
        return False
    if a["log_file"] != b["log_file"]:
        return a["log_file"] > b["log_file"]
    return a["log_pos"] > b["log_pos"]


def _event_trend(cur):
    """最近 24h CDC 事件量按小时分桶（含 0 值空桶，便于一眼看出断流时段）。

    分桶键用 DATE()+HOUR() 而不是 DATE_FORMAT：格式串里的 % 在 pymysql 传参路径下会被
    当成占位符，改一次 SQL 加参数就会炸；这里从根上不引入 %。
    空桶补齐的时间基准取 SQL 的 NOW()（服务器时区），全程不与宿主 UTC 混算。
    """
    cur.execute("SELECT NOW()")
    server_now = cur.fetchone()[0]
    cur.execute(
        "SELECT DATE(cdc_ts), HOUR(cdc_ts), COUNT(*) FROM ods_cdc_log "
        "WHERE cdc_ts >= NOW() - INTERVAL 24 HOUR GROUP BY 1,2"
    )
    got = {(str(r[0]), int(r[1])): int(r[2]) for r in cur.fetchall()}
    buckets = []
    for i in range(23, -1, -1):
        t = server_now - timedelta(hours=i)
        key = (t.strftime("%Y-%m-%d"), t.hour)
        buckets.append({"hour": f"{t.hour:02d}:00", "count": got.get(key, 0)})
    return buckets


# ---------------- handlers ----------------


def get_datasource(ctx):
    """页面主接口：登记表 + 分层健康度 + CDC 状态 + 事件趋势 + 告警。"""
    _ensure_registry()
    conn = db.crawl_conn()
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT source_id,name,source_type,endpoint,cdc_enabled,data_level,owner,enabled,"
            f"target_table,service_unit,remark,updated_at FROM {REGISTRY_TABLE} "
            "ORDER BY FIELD(source_type,'业务库','日志','爬虫','空间API'), source_id"
        )
        cols = [d[0] for d in cur.description]
        sources = []
        for r in cur.fetchall():
            row = dict(zip(cols, r, strict=True))
            row["cdc_enabled"] = bool(row["cdc_enabled"])
            row["enabled"] = bool(row["enabled"])
            row["updated_at"] = str(row["updated_at"])
            sources.append(row)

        cdc = _cdc_state(cur)
        probe = _source_probe()
        cdc["master"] = probe["master"]

        # ---- CDC 健康判定：区分强弱信号，避免把「静默」误报成「断流」 ----
        # 弱信号 · 主库位点领先：tools/cdc/main.py::_check_alerts 用的就是这条，但它在本环境
        #   会常态成立——binlog 记录的是**整个实例**的写入，而 CDC 只订阅 spacefin 库的三张表；
        #   spacefin_crawler 侧每写一次 ods_cdc_log/dws_risk_class，主库位点就往前跑。
        #   所以「领先」只能当参考，不能当断流结论，否则页面天天飘红。
        # 强信号 · 下游积压：consumer lag 是 ODS 内部口径，不受其它库写入干扰。
        # 强信号 · 源头更新但 ODS 没跟上：业务表最新行比 ODS 最后一条事件还新 = 确定漏采。
        pos = cdc["position"]
        master_ahead = _pos_ahead(probe["master"], pos) if (probe["master"] and pos) else None
        stale = pos["stale_seconds"] if pos else None
        max_lag = max((c["lag"] for c in cdc["consumers"]), default=0)

        cur.execute("SELECT TIMESTAMPDIFF(SECOND, MAX(cdc_ts), NOW()) FROM ods_cdc_log")
        row = cur.fetchone()
        ods_age = int(row[0]) if row and row[0] is not None else None
        biz_age = probe["biz_newest_age"]
        # 留 60s 容差：CDC 从 binlog 到落 ODS 本身有秒级时延，卡太死会误报。
        missed = biz_age is not None and ods_age is not None and biz_age + 60 < ods_age

        # 只有强信号能定级；弱信号单独出一条 stall_suspect 提示，在卡片里明说它是弱信号。
        # 阈值 1000 与 tools/cdc/main.py 的 CONSUMER_LAG_THRESHOLD 对齐，页面与后台告警同尺子。
        level = "bad" if (missed or max_lag > 1000) else "ok"
        stall_suspect = bool(master_ahead) and stale is not None and stale > 1800

        cdc["verdict"] = {
            "level": level,
            "stall_suspect": stall_suspect,
            "missed_events": missed,
            "master_ahead": master_ahead,
            "stale_seconds": stale,
            "max_lag": max_lag,
            "ods_age_seconds": ods_age,
            "biz_newest_age_seconds": biz_age,
            # 下游全部追平 = 链路里没有滞留事件，「没有新事件」是源头没变更。
            # ODS 层的新鲜度判定要用到它：否则业务库空闲时 ODS 常年标红，告警一失真就没人看了。
            "idle": level == "ok" and max_lag == 0,
            "master_note": (
                "主库 binlog 位点涵盖实例全部库表的写入，而 CDC 只订阅 spacefin 的 "
                "loan/collateral/customer——位点领先属常态，仅作弱信号参考，不作断流结论。"
            ),
        }

        trend = _event_trend(cur)

        cur.execute("SELECT NOW()")
        server_now = str(cur.fetchone()[0])
        cur.close()

        return {
            "sources": sources,
            "levels": DATA_LEVELS,
            "types": SOURCE_TYPES,
            "cdc": cdc,
            "trend": trend,
            "server_now": server_now,
            "can_edit": ctx.user["role"] == "admin",
        }
    finally:
        conn.close()


def toggle_datasource(ctx):
    """切换某个数据源的 cdc_enabled / enabled（仅管理员）。

    只允许改这两个布尔位：endpoint/data_level 属于合规登记内容，改动要走评审留档，
    不适合在页面上随手点（分级标签一旦被随意下调，导出脱敏规则就形同虚设）。
    """
    if ctx.user["role"] != "admin":
        return 403, {"error": "无权限"}
    _ensure_registry()

    source_id = (ctx.body.get("source_id") or "").strip()
    field = (ctx.body.get("field") or "").strip()
    value = ctx.body.get("value")
    if field not in ("cdc_enabled", "enabled"):
        raise ValueError("field 只支持 cdc_enabled / enabled")
    if value not in (0, 1, True, False):
        raise ValueError("value 只支持 0 / 1")
    if not source_id:
        raise ValueError("缺少 source_id")

    conn = db.crawl_conn()
    try:
        cur = conn.cursor()
        # field 已被上面的白名单收敛为两个固定字面量，此处拼接无注入面；source_id 仍走参数化。
        cur.execute(
            f"UPDATE {REGISTRY_TABLE} SET {field}=%s WHERE source_id=%s",
            (1 if value in (1, True) else 0, source_id),
        )
        conn.commit()
        changed = cur.rowcount
        cur.close()
    finally:
        conn.close()
    if changed == 0:
        # rowcount=0 也可能是「值没变」，先确认这行到底在不在，避免把幂等点击报成不存在。
        conn = db.crawl_conn()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {REGISTRY_TABLE} WHERE source_id=%s", (source_id,))
            exists = int(cur.fetchone()[0])
            cur.close()
        finally:
            conn.close()
        if not exists:
            return 404, {
                "error": f"数据源不存在：{source_id}",
                "error_key": "数据源不存在：{source_id}",
                "error_params": {"source_id": source_id},
            }

    # 配置变更留痕，与导出/确认走同一张 ads_export_audit（TC-06 口径）。
    # ip 由框架经 RouteCtx 下发（R-UNW-02：变更须可追溯到来源）。
    db.write_audit(
        "datasource_toggle",
        ctx.user["user"],
        ctx.user["role"],
        json.dumps({"source_id": source_id, "field": field, "value": value}, ensure_ascii=False),
        "success",
        getattr(ctx, "ip", "-"),
    )
    return {
        "ok": True,
        "source_id": source_id,
        "field": field,
        "value": 1 if value in (1, True) else 0,
    }


PAGE = {
    "id": "datasource",
    "label": "数据底座接入配置",
    # 仅 admin 可配置数据底座（目标用户 = 数据工程师；历史 da 角色已移除）。
    "roles": {"admin"},
    "order": 20,
    "js": "p1_datasource.js",
    "routes": {
        ("GET", "/api/datasource"): get_datasource,
        ("POST", "/api/datasource/toggle"): toggle_datasource,
    },
}
