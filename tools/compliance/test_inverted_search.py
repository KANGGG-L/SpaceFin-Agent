"""I-04 倒排索引审计检索测试（需 Doris 在线，9030）。

Doris 不可达时自动跳过，避免 CI 在无 Doris 环境失败。
运行：pytest tools/compliance/test_inverted_search.py -v
"""

import pytest

from tools.compliance.inverted_search import (
    DATABASE,
    INDEX_NAME,
    TABLE,
    connect,
    ensure_audit_table,
    search_audit,
)


def _doris_reachable() -> bool:
    try:
        conn = connect()
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _doris_reachable(), reason="Doris FE (127.0.0.1:9030) 不可达，跳过 I-04 实跑测试"
)


@pytest.fixture(scope="module")
def prepared():
    conn = connect()
    try:
        ensure_audit_table(conn)
    finally:
        conn.close()
    return True


def test_table_and_index_exist(prepared):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW TABLES FROM {DATABASE} LIKE '{TABLE}'")
            assert cur.fetchone() is not None, f"{TABLE} 应存在"
            cur.execute(f"SHOW INDEX FROM {TABLE}")
            idx = [r[2] for r in cur.fetchall()]
        assert INDEX_NAME in idx, f"倒排索引 {INDEX_NAME} 未建立"
    finally:
        conn.close()


def test_search_packaging_flow(prepared):
    rows, elapsed_ms = search_audit("包装流水")
    assert len(rows) >= 1, "应命中含『包装流水』的样例行"
    assert any("包装流水" in (r["audit_text"] or "") for r in rows)
    assert elapsed_ms < 1000.0, f"检索应毫秒级，实际 {elapsed_ms:.2f}ms"
    print(f"[I-04] 包装流水 hits={len(rows)} elapsed={elapsed_ms:.2f}ms")


def test_search_no_false_positive(prepared):
    rows, _ = search_audit("量子计算")
    assert len(rows) == 0, "无关词不应命中任何行"
