"""沙箱的行为校验：能查、不能写、跑不飞、不撑爆 context。"""

from __future__ import annotations

from sandbox.executor import SQLiteSandbox
from sandbox.guard import check


def test_select_returns_rows(sales_db):
    r = SQLiteSandbox(sales_db).run("SELECT COUNT(*) FROM orders")
    assert r.ok and r.rows == [(5,)]


def test_join_and_aggregate(sales_db):
    r = SQLiteSandbox(sales_db).run(
        "SELECT c.region, SUM(o.amount) FROM orders o "
        "JOIN customers c ON c.id = o.customer_id "
        "WHERE o.status = 'paid' GROUP BY c.region"
    )
    assert r.ok
    assert dict(r.rows) == {"East": 250.5}


def test_write_is_rejected_before_execution(sales_db):
    """写操作应该在 guard 层就被拒，压根不该到数据库。"""
    r = SQLiteSandbox(sales_db).run("DELETE FROM orders")
    assert not r.ok and "被安全策略拒绝" in r.error


def test_readonly_connection_is_the_second_line_of_defence(sales_db):
    """即使绕过 guard 直接执行，只读连接也必须拦住写操作。"""
    sb = SQLiteSandbox(sales_db)
    conn = sb._connect()
    try:
        raised = False
        try:
            conn.execute("DELETE FROM orders")
        except Exception:
            raised = True
        assert raised, "只读连接竟然允许了写操作"
    finally:
        conn.close()


def test_bad_column_error_is_returned_not_raised(sales_db):
    """报错要原样返回，这是执行反馈重试的输入。"""
    r = SQLiteSandbox(sales_db).run("SELECT nonexistent FROM orders")
    assert not r.ok
    assert "nonexistent" in r.error


def test_row_cap_truncates_instead_of_flooding_context(sales_db):
    r = SQLiteSandbox(sales_db, max_rows=2).run("SELECT id FROM orders")
    assert r.ok and len(r.rows) == 2 and r.truncated


def test_missing_limit_is_injected_with_one_spare_row(sales_db):
    """多取一行是"结果被截断了"的判据，所以注入的上限是 max_rows + 1。"""
    r = SQLiteSandbox(sales_db, max_rows=3).run("SELECT id FROM orders")
    assert "LIMIT 4" in r.sql
    assert len(r.rows) == 3 and r.truncated


def test_existing_limit_is_preserved(sales_db):
    """模型可能故意要 top-N，不能被护栏改写。"""
    r = SQLiteSandbox(sales_db, max_rows=999).run("SELECT id FROM orders LIMIT 2")
    assert r.ok and len(r.rows) == 2 and "LIMIT 2" in r.sql


def test_multi_statement_is_blocked(sales_db):
    r = SQLiteSandbox(sales_db).run("SELECT 1; DROP TABLE orders")
    assert not r.ok and "单条语句" in r.error


def test_cte_is_allowed(sales_db):
    r = SQLiteSandbox(sales_db).run(
        "WITH paid AS (SELECT * FROM orders WHERE status='paid') "
        "SELECT COUNT(*) FROM paid"
    )
    assert r.ok and r.rows == [(3,)]


def test_guard_blocks_file_functions():
    assert not check("SELECT readfile('/etc/passwd')").ok


def test_markdown_rendering_marks_empty_and_error(sales_db):
    sb = SQLiteSandbox(sales_db)
    assert "没有返回任何行" in sb.run("SELECT id FROM orders WHERE id = -1").to_markdown()
    assert sb.run("SELECT bad FROM orders").to_markdown().startswith("ERROR:")
