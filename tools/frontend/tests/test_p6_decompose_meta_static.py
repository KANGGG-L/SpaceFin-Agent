"""P6 静态验收：decompose 正常态的「模型来源披露」（config 展示补齐）。

此前只有降级态有横幅披露，LLM 成功时反而沉默——用户无从知道草稿出自哪个
模型、耗时多少。本组断言把「正常态也要展示来源」钉进三处：
1. 后端 ai_guard：_nl_llm_request 成功路径必须写 meta.model / meta.elapsed_ms；
2. 后端 ai_decompose：成功响应必须携带 meta（含 cached 标记），缓存命中不丢；
3. 前端 p6_policy.js：#p6-decompose-src 槽位存在、状态捕获与渲染逻辑齐全。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
JS = ROOT / "tools" / "frontend" / "static" / "pages" / "p6_policy.js"
GUARD = ROOT / "tools" / "frontend" / "ai_guard.py"
DECOMP = ROOT / "tools" / "frontend" / "ai_decompose.py"


def test_guard_records_model_meta_on_success():
    src = GUARD.read_text(encoding="utf-8")
    assert 'meta["model"] = model' in src, "_nl_llm_request 成功路径必须记录模型名"
    assert 'meta["elapsed_ms"]' in src, "成功路径必须记录耗时（展示用）"


def test_decompose_response_carries_meta():
    src = DECOMP.read_text(encoding="utf-8")
    assert '"meta": {' in src, "decompose 成功响应必须携带 meta"
    assert '"model": meta.get("model")' in src
    assert '"cached": bool(meta.get("cached"))' in src, "缓存命中也要如实标注"
    # 缓存值必须带 meta：命中后展示层才有来源可读
    assert 'ai_guard._nl_cache_put(cache_key, {"obj": raw, "meta": dict(meta)})' in src
    # 缓存写入必须在语言红线（_free_text_is_cjk_dominant）之后：被红线拒绝的
    # 原始输出绝不能入缓存，否则降级被钉死、模型行为改善也救不回来。
    lang_gate = src.index("_free_text_is_cjk_dominant(valid")
    cache_put = src.index('ai_guard._nl_cache_put(cache_key, {"obj": raw')
    assert cache_put > lang_gate, "缓存写入必须晚于语言红线校验"


def test_frontend_source_slot_and_state():
    js = JS.read_text(encoding="utf-8")
    assert 'id="p6-decompose-src"' in js, "缺少来源展示槽位"
    assert "st.decomposeMeta = (dc.meta" in js, "runDecompose 未捕获响应 meta"
    # 复位点：catch / discard / invalidate / reset 至少要有 st.decomposeMeta = null 的清场
    assert js.count("st.decomposeMeta = null;") >= 3, "meta 复位点不足（残留会跨草稿串显）"


def test_frontend_source_render_mutually_exclusive_with_banner():
    js = JS.read_text(encoding="utf-8")
    m = re.search(r"const srcEl = el\(\"p6-decompose-src\"\);", js)
    assert m, "缺少来源渲染逻辑"
    # 渲染条件必须含 !st.decomposeDegraded：与降级横幅互斥，不得同时出现
    seg = js[m.start() : m.start() + 700]
    assert "!st.decomposeDegraded && m && m.model" in seg
    assert 't("由 {model} 生成（缓存复用）"' in seg and 't("由 {model} 生成 · {n}s"' in seg
