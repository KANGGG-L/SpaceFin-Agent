"""dev_server 静态回归防线（SF_HOTRELOAD 清洗，F5）。

F5：`dev_server.py` 文档写明刻意不注入 SF_HOTRELOAD，但「不注入」不等于「清洗」——`load_env()`
以 `dict(os.environ)` 起始，shell 里继承来的 SF_HOTRELOAD=1 会原样漏给孩子，于是监督器 mtime 重启
与 app.py 在请求处理器内的 os.execv 同时武装 ⇒ 一次编辑两次重启。

本文件是**源码级**断言，随 `dev_server.py` 同批提交；瓦片侧用例（F1/F2/F3/F4/vieww）在
`test_p5_basemap_static.py`，保证「瓦片 commit」与「dev commit」各自独立全绿。
"""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # tests/ 的上三级 = 仓库根

#: §6.4 要求「先红」——把 HEAD 版本 `git show HEAD:dev_server.py > /tmp/…` 后用这个环境变量把解析目标
#: 指过去跑一次，证明修复前用例确实 fail。默认（未设置时）永远指向真实仓库文件，不允许借它放宽判定。
_DS_OVERRIDE = os.environ.get("SF_P5_DEV_SERVER_SRC")


def _dev_server_text() -> str:
    p = Path(_DS_OVERRIDE) if _DS_OVERRIDE else ROOT / "dev_server.py"
    return p.read_text(encoding="utf-8")


def test_dev_server_scrubs_inherited_sf_hotreload_before_spawning():
    """F5：`dev_server.py` 文档写明刻意不注入 SF_HOTRELOAD，但「不注入」不等于「清洗」。

    `load_env()` 以 `dict(os.environ)` 起始，shell 里继承来的 SF_HOTRELOAD=1 会原样漏给孩子，
    于是监督器 mtime 重启与 app.py 在请求处理器内的 os.execv 同时武装 ⇒ 一次编辑两次重启。
    """
    src = _dev_server_text()
    hits = [ln for ln in src.splitlines() if 'env.pop("SF_HOTRELOAD"' in ln]
    assert hits, (
        'dev_server.py 未把 SF_HOTRELOAD 从传给子进程的 env 中摘掉（应为 env.pop("SF_HOTRELOAD", None)）'
    )
    assert any(not ln.strip().startswith("#") for ln in hits), (
        "SF_HOTRELOAD 的摘除只出现在注释行里＝意图写在文档、代码没做（F5 根因）"
    )
    # 清洗必须落在 main() 真正交给 Proc 的那份 env 上。
    # （不能按文本先后去比 subprocess.Popen：Proc.start() 定义在 main() 之前却调用在其之后，那样判是错的。）
    main_body = src[src.index("def main(") :]
    assert re.search(r"^\s*env = load_env\(\)", main_body, re.M), (
        "main() 不再从 load_env() 取子进程 env"
    )
    assert 'env.pop("SF_HOTRELOAD", None)' in main_body, (
        "摘除语句不在 main() 内 ⇒ 没作用在交给子进程的那份 env 上（F5 根因仍在）"
    )
    assert main_body.index('env.pop("SF_HOTRELOAD", None)') < main_body.index("procs = []"), (
        "摘除发生在构造 Proc 列表之后 ⇒ Proc 拿到的 env 仍带着继承来的 SF_HOTRELOAD"
    )
    assert re.search(r"def load_env\(\)[\s\S]{0,240}?env = dict\(os\.environ\)", src), (
        "load_env() 不再以 dict(os.environ) 起始＝有人把清洗挪走或改成了白名单语义，F5 判定前提失效"
    )
