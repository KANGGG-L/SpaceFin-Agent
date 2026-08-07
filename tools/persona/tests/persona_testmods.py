"""加载 tools/persona 的平铺模块。测试从这里取被测对象。

为什么不用 `from conftest import ...`：仓库里已有多个 tools/*/tests/conftest.py
都用 `from conftest import ...`，而 tests 目录没有 __init__.py，pytest 的 prepend
导入模式下 `conftest` 是平铺模块名，同一会话跑多个目录时先到先得，后面的目录会
拿到别人的 conftest。本文件用唯一模块名规避该冲突（同 tools/risk/tests/riskmods.py
的写法），跨目录整仓跑不受影响。

同理，tools/persona 用 `import benchmark` / `import generator` 这类平铺导入，
而仓库里多个工具目录都有同名 main.py 等。加载前先清掉这些平铺名并把
tools/persona 顶到 sys.path[0]；模块加载完后在自己的 globals 里持有正确引用。
"""

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PERSONA_DIR = os.path.dirname(TESTS_DIR)

_FLAT_NAMES = ("benchmark", "generator", "critic", "main")


def _load():
    for name in _FLAT_NAMES:
        sys.modules.pop(name, None)
    while PERSONA_DIR in sys.path:
        sys.path.remove(PERSONA_DIR)
    sys.path.insert(0, PERSONA_DIR)

    import benchmark as _benchmark
    import critic as _critic
    import generator as _generator

    return _benchmark, _generator, _critic


benchmark, generator, critic = _load()
