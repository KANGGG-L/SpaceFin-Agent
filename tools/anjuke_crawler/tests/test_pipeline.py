"""端到端流水线测试：三段链路（分布式队列 + 代理池对接 + 抓取解析）整体跑通。"""

import os
import subprocess
import sys


def test_demo_pipeline_end_to_end():
    tools_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    r = subprocess.run(
        [sys.executable, "-m", "anjuke_crawler.scripts.demo_pipeline"],
        capture_output=True,
        text=True,
        cwd=tools_dir,
        timeout=300,
    )
    assert r.returncode == 0, f"demo 退出码 {r.returncode}\n{r.stdout}\n{r.stderr}"
    # 三段都应有成功标记
    assert "段 1/3" in r.stdout and "段 2/3" in r.stdout and "段 3/3" in r.stdout
    assert "[OK] 三段链路" in r.stdout
    assert "Exported 71 clean records" in r.stdout  # 分布式段导出 71 条
