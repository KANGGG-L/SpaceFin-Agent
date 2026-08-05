"""加载 tools/spatial 的平铺模块。测试从这里取被测对象。

命名与加载方式的原因同 tools/risk/tests/riskmods.py：用唯一模块名导出被测对象，
避免多个 tests 目录同时收集时 `from conftest import ...` 串到别的目录（仓库现存问题）；
tools/spatial/config.py 与 tools/risk/config.py 平铺同名，加载前先清掉平铺名，
再把 tools/spatial 顶到 sys.path[0]。
"""

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SPATIAL_DIR = os.path.dirname(TESTS_DIR)

_FLAT_NAMES = ("config", "features", "store", "main")


def _load():
    for name in _FLAT_NAMES:
        sys.modules.pop(name, None)
    while SPATIAL_DIR in sys.path:
        sys.path.remove(SPATIAL_DIR)
    sys.path.insert(0, SPATIAL_DIR)

    import config as _config
    import features as _features

    return _config, _features


config, features = _load()


def sale_row(lat, lng, price, city="gz", community="示例小区", url_key=None):
    """一行 DWD 挂牌（已过滤为有坐标、有有效单价）。

    注意 district 列存的是城市码（见 tools/risk/valuation.load_dwd_unit_prices 的别名）。
    """
    return {
        "lat": lat,
        "lng": lng,
        "unit_price_yuan": price,
        "district": city,
        "community": community,
        "url_key": url_key or f"k-{lat}-{lng}-{price}",
    }


def grid_of(lng, lat):
    """把坐标落到 0.02° 网格左下角，用于构造「同一网格内」的样本。"""
    return config.grid_key(lng, lat)
