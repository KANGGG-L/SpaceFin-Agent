#!/usr/bin/env python
"""兼容入口：pipeline 仍按 g11_report.py 调用 1104 G11 报送，实现收敛在 main.py。

历史原因：早期曾以 tools/reporting/g11_report.py 作为报送入口接线（run_pipeline.py 的
g11 步）。现 G11 生成、三出口校验与落库已统一收口在 tools/reporting/main.py，本文件仅做
转发，避免改断下游对该入口的引用。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
