"""加载 tools/avm 的平铺模块。测试从这里取被测对象。

tools/avm 的模块用平铺导入（train.py 内部 `from data_clean import ...`），且
data_clean/predict/train 三个模块名在仓库内唯一，不存在跨目录撞名。用唯一模块名
avmmods 规避跨目录 conftest 撞名（同 tools/risk/tests/riskmods.py 的约定，
原因见该文件顶部说明）。

隔离原则：本目录不连任何数据库、不加载 output/avm 的真实模型产物。
predict/train/confidence 的用例一律用构造的假 model dict 与假回归器。
"""

import os
import sys

import numpy as np

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
AVM_DIR = os.path.dirname(TESTS_DIR)


def _load():
    while AVM_DIR in sys.path:
        sys.path.remove(AVM_DIR)
    sys.path.insert(0, AVM_DIR)

    import data_clean  # noqa: F401  (import 时载入 community_vocab.json，只读)
    import predict  # noqa: F401
    import train  # noqa: F401

    return data_clean, predict, train


data_clean, predict, train = _load()


# ------------------------------------------------------------------ 假模型产物
class CaptureRegressor:
    """记录每次 predict 的输入特征；返回 log 单价。

    回退语义与训练侧一致：小区中位命中 → 用它；小区未知 → 城市中位；
    城市未知 → 全局中位（_build_row 把未知城市的 city 编码退化为 (g, g, 0)）。
    这样 estimate_total_price 的返回值就反映了喂给模型的特征落到哪一级，
    用例只需断言「总价 = 该级中位价 × 面积」。
    """

    def __init__(self):
        self.rows = []

    def predict(self, x):
        row = np.asarray(x)[0]
        self.rows.append(row)
        if not np.isnan(row[16]):  # 小区中位（含 EB 收缩后的值）
            lp = np.log(row[17])
        elif not np.isnan(row[21]):  # 城市中位
            lp = np.log(row[21])
        else:  # 全局中位
            lp = np.log(row[20])
        return np.array([lp])


def make_model(smooth_k=10.0, smooth_mode=None, eb_k=None):
    """构造最小可用的假 model dict（键与 train.py 产物一致，predict 只依赖这些键）。"""
    enc = {
        "global": 40000.0,
        "city": {
            "gz": (43000.0, 42000.0, 500),
            "sz": (60000.0, 59000.0, 400),
        },
        "comm": {
            ("gz", "天河城"): (50000.0, 48000.0, 20),
        },
    }
    if eb_k:
        enc["eb_k"] = eb_k
    artifact = {
        "model": CaptureRegressor(),
        "encoders": enc,
        "cities": ["gz", "sz"],
        "smooth_k": smooth_k,
        "version": "2026-08-05-test",
    }
    if smooth_mode:
        artifact["smooth_mode"] = smooth_mode
    return artifact
