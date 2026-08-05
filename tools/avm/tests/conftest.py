"""tools/avm 测试的公共 fixture（不连数据库、不加载真实模型产物）。"""

import avmmods
import numpy as np
import pytest


# ------------------------------------------------------------------ 楼盘名词典隔离
@pytest.fixture
def empty_vocab():
    """把楼盘名词典置空，保证 parse_community_from_title 的尾缀规则用例确定性。

    解析器先走尾缀规则、失败才查语料词典（community_vocab.json 落盘文件在 import 时
    自动载入）。置空词典后「解析得出/解析不出」完全由尾缀规则决定，不依赖落盘数据。
    """
    dc = avmmods.data_clean
    saved = (dc._NAME_VOCAB, dc._NAME_CANON, dc._VOCAB_FREQ)
    dc.install_name_vocab(set(), {}, {})
    yield
    dc.install_name_vocab(*saved)


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


@pytest.fixture
def model():
    """默认假模型：smooth_k=10，小区「天河城」(n=20, 中位 48000)、gz 城市中位 42000、
    全局中位 40000。

    小区中位经 EB 收缩：sm_med = (20·48000 + 10·42000)/30 = 46000。
    """
    return make_model()


@pytest.fixture
def reg(model):
    """拿到假回归器实例以检查捕获的特征行。"""
    return model["model"]
