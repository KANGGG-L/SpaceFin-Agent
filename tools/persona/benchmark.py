"""基准分布快照：从 MySQL 抽取 spacefin.customer 三特征（附行业/城市维度）并落盘（运行不再依赖 MySQL）。

派生违约概率公式（本模块固定，与 benchmark_customer.json 内嵌的公式一致）：

    p_default = sigmoid(b0 + b1 * debt_ratio + b2 * (credit_score - score_center))
    b0 = -2.0, b1 = 3.0, b2 = -0.006, score_center = 680.0

- debt_ratio 越高 → logit 越大 → 违约率越高；
- credit_score 越高 → logit 越小（b2 < 0）→ 违约率越低；
- income_monthly 不参与违约率公式，仅作为画像特征参与 KS 检测与校准。

参数为演示「生成 → 批评 → 校准」机制而选取的合理值：在合成种子基准 200 行上
违约率均值约 0.36、p10-p90 约 0.17-0.56，分布有足够区分度，能明显暴露美化偏见
造成的违约率偏移。

快照 benchmark_customer.json 已提交进 repo，业务运行（main / 测试）只走
load_benchmark() 离线加载；extract_from_mysql() 仅用于重新生成快照。
"""

import datetime
import json
import os
import subprocess
from dataclasses import dataclass

import numpy as np
from scipy.special import expit

# ------------------------------------------------------------------ 违约率公式常量
LOGISTIC = {
    "b0": -2.0,
    "b1": 3.0,
    "b2": -0.006,
    "score_center": 680.0,
}

FEATURE_KEYS = ("income_monthly", "debt_ratio", "credit_score")

DEFAULT_SNAPSHOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "benchmark_customer.json"
)


def derive_default_prob(debt_ratio, credit_score):
    """按固定 logistic 公式派生违约概率。

    违约率随 debt_ratio 单调升、随 credit_score 单调降（见模块 docstring 的公式）。
    入参可为标量或数组，返回与入参同 shape 的 float / ndarray。
    """
    logit = (
        LOGISTIC["b0"]
        + LOGISTIC["b1"] * np.asarray(debt_ratio)
        + LOGISTIC["b2"] * (np.asarray(credit_score) - LOGISTIC["score_center"])
    )
    return expit(logit)


@dataclass
class Benchmark:
    """基准分布：三特征数组 + 派生违约概率 + 来源元信息。

    industry / city 是每行基准客户的属性维度（非 KS 特征）：industry 为借款人
    行业 slug，city 为抵押物地址所在城市（脱敏到市级，来自 loan→collateral 关联）。
    两者可选（旧快照没有）：为 None 时下游画像不携带该维度。
    """

    income_monthly: np.ndarray
    debt_ratio: np.ndarray
    credit_score: np.ndarray
    source: str = ""
    extracted_at: str = ""
    industry: list = None
    city: list = None

    @property
    def n(self):
        return len(self.income_monthly)

    @property
    def default_prob(self):
        """派生违约概率（由固定公式即时算出，单一事实来源在代码里）。"""
        return derive_default_prob(self.debt_ratio, self.credit_score)

    @property
    def features(self):
        return {
            "income_monthly": self.income_monthly,
            "debt_ratio": self.debt_ratio,
            "credit_score": self.credit_score,
        }


# ------------------------------------------------------------------ MySQL 抽取
def extract_from_mysql(env_path=None, container="spacefin-mysql", password=None):
    """用 docker exec 读 spacefin.customer 的三特征 + 行业/城市，返回 Benchmark。

    行业/城市是画像的属性维度（供规则影响判读，不参与 KS/违约率公式）：
    - industry：customer.industry 的行业 slug；
    - city：客户唯一贷款的抵押物地址城市前缀（脱敏到市级，如「广州市」），
      经 loan→collateral 关联取得——customer 表本身没有地址列。
    客户:贷款为 1:1（种子约束），LEFT JOIN 不会翻倍行数；无贷款/抵押物的
    客户两列为 NULL，保留行（三特征仍是基准的一部分）。

    密码优先级：显式 password 参数 > .env 的 MYSQL_ROOT_PASSWORD。env_path 缺省时
    自动找 repo 根 .env（tools/persona 的上级目录）。
    """
    if password is None:
        env_path = env_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"
        )
        password = _read_env_password(env_path)

    sql = (
        "SELECT c.income_monthly, c.debt_ratio, c.credit_score, c.industry, "
        "SUBSTRING(col.property_addr, 1, 3) "
        "FROM spacefin.customer c "
        "LEFT JOIN spacefin.loan l ON l.customer_id = c.customer_id "
        "LEFT JOIN spacefin.collateral col ON col.collateral_id = l.collateral_id"
    )
    cmd = [
        "docker",
        "exec",
        container,
        "mysql",
        "--default-character-set=utf8mb4",
        "-uroot",
        f"-p{password}",
        "-N",
        "-B",
        "-e",
        sql,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker exec mysql 失败（exit={proc.returncode}）：{proc.stderr.strip()[:300]}"
        )
    rows = [line.split("\t") for line in proc.stdout.strip().splitlines() if line.strip()]
    if len(rows) < 2:
        raise RuntimeError("spacefin.customer 无数据（或解析异常），请确认 seed 已灌入。")

    income, debt, score, industry, city = [], [], [], [], []
    for r in rows:
        if len(r) < 3:
            continue
        income.append(float(r[0]))
        debt.append(float(r[1]))
        score.append(float(r[2]))
        # -B 模式下 NULL 就是字面量 "NULL"；只有全列一致时才按 NULL 处理。
        industry.append(None if len(r) < 4 or r[3] == "NULL" else r[3])
        city.append(None if len(r) < 5 or r[4] == "NULL" else r[4])
    return Benchmark(
        income_monthly=np.asarray(income, dtype=float),
        debt_ratio=np.asarray(debt, dtype=float),
        credit_score=np.asarray(score, dtype=float),
        source=f"{container}: spacefin.customer",
        industry=industry,
        city=city,
    )


def _read_env_password(env_path):
    if not os.path.exists(env_path):
        raise RuntimeError(f".env 不存在：{env_path}")
    for line in open(env_path, encoding="utf-8"):
        line = line.strip()
        if line.startswith("MYSQL_ROOT_PASSWORD="):
            return line.split("=", 1)[1]
    raise RuntimeError(".env 中未找到 MYSQL_ROOT_PASSWORD")


# ------------------------------------------------------------------ 快照读写
def save_snapshot(benchmark, path=None, extracted_at=None):
    """把基准三特征数组 + 派生违约概率 + 公式落盘为 JSON（提交进 repo 的快照）。"""
    path = path or DEFAULT_SNAPSHOT
    doc = {
        "source": benchmark.source,
        "extracted_at": extracted_at or datetime.date.today().isoformat(),
        "n": benchmark.n,
        "formula": {
            "kind": "logistic",
            "equation": "p_default = sigmoid(b0 + b1*debt_ratio + b2*(credit_score - score_center))",
            **LOGISTIC,
        },
        "features": {
            "income_monthly": [round(float(x), 4) for x in benchmark.income_monthly],
            "debt_ratio": [round(float(x), 4) for x in benchmark.debt_ratio],
            "credit_score": [round(float(x), 4) for x in benchmark.credit_score],
            # 属性维度（非 KS 特征）：供明细表判读规则影响；城市脱敏到市级。
            "industry": benchmark.industry or [],
            "city": benchmark.city or [],
        },
        "derived_default_prob": [round(float(x), 6) for x in benchmark.default_prob],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return path


def load_benchmark(path=None):
    """离线加载基准快照（零 DB 依赖）。违约概率始终由代码内固定公式重算。"""
    path = path or DEFAULT_SNAPSHOT
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    feats = doc["features"]
    return Benchmark(
        income_monthly=np.asarray(feats["income_monthly"], dtype=float),
        debt_ratio=np.asarray(feats["debt_ratio"], dtype=float),
        credit_score=np.asarray(feats["credit_score"], dtype=float),
        source=doc.get("source", ""),
        extracted_at=doc.get("extracted_at", ""),
        industry=feats.get("industry") or None,
        city=feats.get("city") or None,
    )


def refresh_snapshot(path=None):
    """从 MySQL 重新抽取并覆盖快照，返回 (benchmark, path)。"""
    bench = extract_from_mysql()
    save_snapshot(bench, path=path)
    return bench, (path or DEFAULT_SNAPSHOT)


if __name__ == "__main__":
    bench, snap = refresh_snapshot()
    print(f"已抽取 {bench.n} 行基准并落盘：{snap}")
