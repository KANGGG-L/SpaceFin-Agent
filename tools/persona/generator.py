"""合成画像生成器：固定 random seed，可复现产出 N 个 persona（三特征 + 违约概率）。

bias 开关模拟「美化偏见」（RLHF 优化导致生成画像偏乐观）：

- bias="naive"：income 上移 ×1.2、debt_ratio 下移 ×0.8（乐观画像），违约概率因
  特征美化被系统性压低——对应 PRD 痛点 P3「LLM 生成画像被美化偏见污染，违约低估」。
- bias="realistic"：贴近基准分布——对基准 200 行做确定性自助采样（无美化变换），
  违约率分布与基准几乎一致。

生成的违约概率一律由 benchmark.derive_default_prob() 按固定公式从特征重算：
生成器只产出特征，违约概率是「行为模型」的输出，批评者（critic）也用它对照基准，
双方口径一致，偏见因此可被 KS 检测。
"""

import os
from dataclasses import dataclass

import benchmark
import numpy as np

DEFAULT_SEED = 42

# 美化偏见的特征变换（bias="naive"）：收入上移、负债率下移，违约概率被压低
NAIVE_INCOME_FACTOR = 1.2
NAIVE_DEBT_FACTOR = 0.8

DEFAULT_SNAPSHOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "benchmark_customer.json"
)


@dataclass
class Personas:
    """一批合成画像：三特征数组 + 派生违约概率 + 生成元信息。"""

    income_monthly: np.ndarray
    debt_ratio: np.ndarray
    credit_score: np.ndarray
    bias: str
    seed: int

    @property
    def n(self):
        return len(self.income_monthly)

    @property
    def default_prob(self):
        return benchmark.derive_default_prob(self.debt_ratio, self.credit_score)

    @property
    def features(self):
        return {
            "income_monthly": self.income_monthly,
            "debt_ratio": self.debt_ratio,
            "credit_score": self.credit_score,
        }


def generate_personas(n=1000, bias="realistic", seed=DEFAULT_SEED, benchmark_data=None):
    """生成 N 个合成画像。

    参数：
        n: 画像数量
        bias: "naive"（美化偏见）/ "realistic"（贴近基准）
        seed: 随机种子，固定后结果可复现
        benchmark_data: Benchmark 对象；缺省时离线加载 repo 内快照（零 DB 依赖）

    返回：
        Personas 对象，含三特征数组与派生违约概率。
    """
    bench = (
        benchmark_data if benchmark_data is not None else benchmark.load_benchmark(DEFAULT_SNAPSHOT)
    )
    rng = np.random.default_rng(seed)

    # 以基准分布为底本做自助采样：保证真实感、且 naive/realistic 只差「美化变换」这一项
    idx = rng.integers(0, bench.n, size=n)
    income = bench.income_monthly[idx]
    debt = bench.debt_ratio[idx]
    score = bench.credit_score[idx]

    if bias == "naive":
        income = income * NAIVE_INCOME_FACTOR
        debt = np.clip(debt * NAIVE_DEBT_FACTOR, 0.0, 1.0)  # 负债率美化下移，违约概率被压低
    elif bias == "realistic":
        pass  # 贴近基准：不做美化变换
    else:
        raise ValueError(f"未知 bias：{bias!r}（可选 naive / realistic）")

    return Personas(
        income_monthly=income,
        debt_ratio=debt,
        credit_score=score,
        bias=bias,
        seed=seed,
    )
