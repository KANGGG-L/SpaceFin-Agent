#!/usr/bin/env python
"""兼容入口：pipeline 仍按 dispatch.py 调用预警推送，实现收敛在 main.py。

历史原因：早期曾以 tools/alerting/dispatch.py 作为推送入口接线（run_pipeline.py 的
alert 步）。现推送清单生成、T+1 去重、失败重试与 driver 装配已统一收口在
tools/alerting/main.py，本文件仅做转发，避免改断下游对该入口的引用。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
