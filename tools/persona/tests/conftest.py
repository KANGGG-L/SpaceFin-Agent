"""tools/persona 测试的公共 fixture（零 DB：全部用 repo 内 benchmark_customer.json）。

被测模块统一经 persona_testmods 加载（唯一模块名，规避平铺 conftest 撞名，
原因见该文件顶部说明）。整个目录不连 MySQL、不跑 docker。
"""

import persona_testmods as m
import pytest


@pytest.fixture(scope="session")
def bench():
    """基准快照（离线加载 repo 内 JSON，零 DB）。"""
    return m.benchmark.load_benchmark()


@pytest.fixture
def naive(bench):
    """bias=naive 的合成画像（美化偏见，固定 seed 可复现）。"""
    return m.generator.generate_personas(n=1000, bias="naive", seed=42, benchmark_data=bench)


@pytest.fixture
def realistic(bench):
    """bias=realistic 的合成画像（贴近基准，固定 seed 可复现）。"""
    return m.generator.generate_personas(n=1000, bias="realistic", seed=42, benchmark_data=bench)
