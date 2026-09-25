"""PostgreSQL 执行器的集成测试。需要本机起测试库：

    docker compose -f tests/pg/docker-compose.yml up -d --wait

连不上就整体跳过，保证没有 Docker 的机器上 ``pytest tests/`` 照样能跑。
连接层的每一道防线都单独验证一次：只测整体的话，某一层失效会被其他层掩盖。
"""

from __future__ import annotations

import os
import socket

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from agent.schema import schema_text
from sandbox.base import Sandbox
from sandbox.postgres import PostgresSandbox, connect_readonly, with_search_path

RO_DSN = os.environ.get(
    "PG_TEST_DSN", "postgresql://agent_ro:agent_ro@127.0.0.1:55432/sales"
)
ADMIN_DSN = os.environ.get(
    "PG_TEST_ADMIN_DSN", "postgresql://postgres:postgres@127.0.0.1:55432/sales"
)


def _reachable() -> bool:
    # libpq 的连接超时最短 2 秒，Windows 上连本机关闭的端口又不会立刻被拒，
    # 没起库时每次跑测试都要白等。先用 socket 探一下端口。
    info = conninfo_to_dict(RO_DSN)
    try:
        socket.create_connection(
            (info.get("host", "127.0.0.1"), int(info.get("port", 5432))), timeout=0.5
        ).close()
    except OSError:
        return False
    try:
        psycopg.connect(RO_DSN, connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(),
    reason="PG 测试库未启动：docker compose -f tests/pg/docker-compose.yml up -d --wait",
)


def test_select_returns_rows():
    r = PostgresSandbox(RO_DSN).run("SELECT COUNT(*) FROM orders")
    assert r.ok and r.rows == [(5,)]


def test_join_and_aggregate():
    r = PostgresSandbox(RO_DSN).run(
        "SELECT c.region, SUM(o.amount) FROM orders o "
        "JOIN customers c ON c.id = o.customer_id "
        "WHERE o.status = 'paid' GROUP BY c.region"
    )
    assert r.ok
    # NUMERIC 回来是 Decimal；比对口径里 Decimal 会转成 float，这里同样按数值比
    assert {k: float(v) for k, v in r.rows} == {"East": 250.5}


def test_mixed_case_identifiers():
    r = PostgresSandbox(RO_DSN).run(
        'SELECT "Product Name" FROM "Products" WHERE "ProductID" = 1'
    )
    assert r.ok and r.rows == [("Widget",)]


def test_write_is_rejected_by_guard():
    r = PostgresSandbox(RO_DSN).run("DELETE FROM orders")
    assert not r.ok and "被安全策略拒绝" in r.error


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "SELECT * INTO t2 FROM orders",
    "SELECT nextval('order_seq')",
    "CREATE TABLE t3 (a int)",
])
def test_readonly_transaction_blocks_writes_even_with_writable_account(sql):
    """只读事务这一层单独验证：账号有写权限、又绕过了 guard，照样写不进去。"""
    conn = connect_readonly(ADMIN_DSN, verify_account=False)
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(sql)
    finally:
        conn.close()


def test_readonly_account_cannot_write_outside_readonly_transaction():
    """账号这一层单独验证：不开只读事务，写操作也要被权限挡住。

    这条同时是在验证 tests/pg/init.sql 的授权写对了——账号自检依赖它。
    """
    conn = psycopg.connect(RO_DSN)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM orders")
    finally:
        conn.close()


def test_writable_account_is_refused():
    """账号不合格就一条都不执行，而不是提醒一下接着跑。"""
    r = PostgresSandbox(ADMIN_DSN).run("SELECT 1")
    assert not r.ok
    assert "超级用户" in r.error and "拒绝执行" in r.error


def test_timeout_is_enforced_by_server():
    r = PostgresSandbox(RO_DSN, timeout=1).run(
        "SELECT COUNT(*) FROM generate_series(1, 2000000000)"
    )
    assert not r.ok and "超时" in r.error
    assert r.elapsed_ms < 10_000


def test_error_is_returned_not_raised_and_hides_cursor_wrapper():
    """报错要原样回灌给模型，但不能带着服务端游标的 DECLARE 包装。"""
    r = PostgresSandbox(RO_DSN).run("SELECT o.idd FROM orders o")
    assert not r.ok
    assert "o.idd" in r.error
    assert "HINT" in r.error  # PG 会提示 "Perhaps you meant ... o.id"，对重试很有用
    assert "DECLARE" not in r.error


def test_row_cap_with_injected_limit():
    """和 SQLite 同一条规则：注入 max_rows + 1，多出的那一行是截断的证据。"""
    r = PostgresSandbox(RO_DSN, max_rows=3).run("SELECT id FROM orders ORDER BY id")
    assert "LIMIT 4" in r.sql
    assert r.ok and len(r.rows) == 3 and r.truncated


def test_exact_row_count_is_not_marked_truncated():
    r = PostgresSandbox(RO_DSN, max_rows=5).run("SELECT id FROM orders")
    assert r.ok and len(r.rows) == 5 and not r.truncated


def test_server_side_cursor_caps_rows_without_limit():
    """没有 LIMIT 兜底时只靠服务端游标取 n 行。

    这条查询有 100 亿行：普通游标要把结果全拉回来，必然超时；服务端游标取几行就返回。
    """
    r = PostgresSandbox(RO_DSN, max_rows=5, timeout=3).run(
        "SELECT a.x, b.y FROM generate_series(1, 100000) a(x), "
        "generate_series(1, 100000) b(y)",
        enforce_limit=False,
    )
    assert r.ok, r.error
    assert len(r.rows) == 5 and r.truncated


def test_satisfies_protocol():
    sb = PostgresSandbox(RO_DSN)
    assert isinstance(sb, Sandbox) and sb.dialect == "postgres"


def test_schema_quotes_identifiers_only_when_needed():
    text = schema_text(RO_DSN)
    assert 'CREATE TABLE "Products" (' in text
    assert '"ProductID" integer PRIMARY KEY' in text
    assert '"Product Name" text' in text
    # 全小写的普通名字不加引号，和 SQLite 的渲染保持一致
    assert "CREATE TABLE orders (" in text
    assert "amount numeric(10,2)" in text


def test_schema_with_sample_rows():
    text = schema_text(RO_DSN, sample_rows=2)
    assert "-- 样例行" in text and "Widget" in text


def test_schema_refuses_writable_account():
    """读 schema 也走同一条只读连接，账号不合格同样拒绝。"""
    with pytest.raises(Exception, match="拒绝执行"):
        schema_text(ADMIN_DSN)


def test_search_path_limits_queries_to_one_schema():
    shop2 = with_search_path(RO_DSN, "shop2")
    assert PostgresSandbox(shop2).run("SELECT COUNT(*) FROM orders").rows == [(2,)]
    assert PostgresSandbox(RO_DSN).run("SELECT COUNT(*) FROM orders").rows == [(5,)]


def test_search_path_limits_schema_text_to_one_schema():
    text = schema_text(with_search_path(RO_DSN, "shop2"))
    assert "CREATE TABLE orders (" in text
    assert "customers" not in text and "Products" not in text


def test_dsn_options_cannot_turn_off_readonly():
    """地址里的 options 要合并进来，但不能把我们强制的只读设置冲掉——哪怕地址里明写了 off。"""
    dsn = with_search_path(RO_DSN, "shop2").replace(
        "options=", "options=-c%20default_transaction_read_only%3Doff%20"
    )
    conn = connect_readonly(dsn)
    try:
        assert conn.execute("SHOW default_transaction_read_only").fetchone() == ("on",)
        assert conn.execute("SHOW search_path").fetchone() == ("shop2",)
    finally:
        conn.close()


def test_cursor_plans_like_direct_execution():
    """游标只负责限行数，不能因为"只取前 10%"的默认假设换一个执行计划、返回不同的行。"""
    conn = connect_readonly(RO_DSN)
    try:
        assert conn.execute("SHOW cursor_tuple_fraction").fetchone() == ("1",)
    finally:
        conn.close()


def test_column_values_on_postgres():
    from agent.schema import column_values

    v = column_values(RO_DSN)
    assert v[("products", "product name")] in (
        "全部取值：'Widget', 'Gadget'", "全部取值：'Gadget', 'Widget'"
    )
    assert v[("orders", "status")].startswith("全部取值：")
