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
