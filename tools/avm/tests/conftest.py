"""tools/avm 测试的公共 fixture（不连数据库、不加载真实模型产物）。

假模型产物（CaptureRegressor / make_model）定义在 avmmods.py，
与其它模块 tests 的唯一模块名约定一致，避免 `from conftest import` 跨目录撞名。
"""

import avmmods
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
@pytest.fixture
def model():
    """默认假模型：smooth_k=10，小区「天河城」(n=20, 中位 48000)、gz 城市中位 42000、
    全局中位 40000。

    小区中位经 EB 收缩：sm_med = (20·48000 + 10·42000)/30 = 46000。
    """
    return avmmods.make_model()


@pytest.fixture
def reg(model):
    """拿到假回归器实例以检查捕获的特征行。"""
    return model["model"]
