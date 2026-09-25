"""guard 在 PostgreSQL / MySQL 方言下的行为。

这些语句在 SQLite 上本来就执行不了，所以只测 SQLite 时发现不了漏洞；
换到服务端库上，它们会建表、改序列、读服务器文件、加锁、挂起连接。
"""

from __future__ import annotations

import pytest

from sandbox.base import Sandbox
from sandbox.executor import SQLiteSandbox
from sandbox.guard import check, ensure_limit

MUST_BLOCK = [
    # PostgreSQL：SELECT INTO 等于 CREATE TABLE AS
    ("postgres", "SELECT * INTO t2 FROM t"),
    ("postgres", "SELECT a FROM t UNION SELECT a INTO x FROM u"),
    # 序列：出现在 SELECT 里，但会改库
    ("postgres", "SELECT nextval('s')"),
    ("postgres", "SELECT setval('s', 1)"),
    # 读服务器文件
    ("postgres", "SELECT pg_read_file('/etc/passwd')"),
    ("postgres", "SELECT pg_read_binary_file('/etc/passwd')"),
    ("postgres", "SELECT pg_ls_dir('.')"),
    ("postgres", "SELECT lo_import('/etc/passwd')"),
    ("postgres", "SELECT lo_export(1, '/tmp/x')"),
    # 挂起、杀连接、改配置——set_config 能把会话只读关掉，等于拆第二道防线
    ("postgres", "SELECT pg_sleep(100)"),
    ("postgres", "SELECT pg_sleep_for('1 hour')"),
    ("postgres", "SELECT pg_terminate_backend(123)"),
    ("postgres", "SELECT pg_cancel_backend(123)"),
    ("postgres", "SELECT set_config('default_transaction_read_only', 'off', false)"),
    ("postgres", "SELECT pg_advisory_lock(1)"),
    ("postgres", "SELECT pg_try_advisory_xact_lock(1)"),
    # 把 SQL 字符串交给函数执行，等于绕过 AST 检查
    ("postgres", "SELECT dblink_exec('dbname=x', 'DELETE FROM t')"),
    ("postgres", "SELECT query_to_xml('SELECT 1', true, true, '')"),
    # 行锁
    ("postgres", "SELECT * FROM t FOR UPDATE"),
    ("postgres", "SELECT * FROM t FOR SHARE"),
    # 带写操作的 CTE
    ("postgres", "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d"),
    # EXPLAIN ANALYZE 在 PG 里会真的执行后面的语句
    ("postgres", "EXPLAIN ANALYZE DELETE FROM t"),
    ("postgres", "COPY t TO PROGRAM 'rm -rf /'"),
    ("postgres", "SET default_transaction_read_only = off"),
    ("postgres", "SET TRANSACTION READ WRITE"),
    # MySQL
    ("mysql", "SELECT LOAD_FILE('/etc/passwd')"),
    ("mysql", "SELECT * FROM t INTO OUTFILE '/tmp/x'"),
    ("mysql", "SELECT * INTO DUMPFILE '/tmp/x' FROM t"),
    ("mysql", "SELECT SLEEP(100)"),
    ("mysql", "SELECT BENCHMARK(1000000000, MD5(1))"),
    ("mysql", "SELECT GET_LOCK('x', 10)"),
    ("mysql", "SELECT sys_exec('rm -rf /')"),
    ("mysql", "SELECT * FROM t FOR UPDATE"),
    ("mysql", "SELECT * FROM t LOCK IN SHARE MODE"),
    ("mysql", "SET SESSION TRANSACTION READ WRITE"),
    ("mysql", "CALL p()"),
    ("mysql", "DO SLEEP(1)"),
]


@pytest.mark.parametrize("dialect,sql", MUST_BLOCK)
def test_dangerous_sql_is_blocked(dialect, sql):
    r = check(sql, dialect=dialect)
    assert not r.ok, f"[{dialect}] 应当拒绝却放行了：{sql}"


# 挡得住还不够，不能误伤正常的分析查询——误伤会直接变成准确率损失。
MUST_ALLOW = [
    ("postgres", "SELECT date_trunc('month', created_at) AS m, SUM(amount) FROM orders GROUP BY 1"),
    ("postgres", 'SELECT "CustomerID", COUNT(*) FROM "Orders" GROUP BY "CustomerID"'),
    ("postgres", "SELECT amount::numeric(10, 2) FROM orders"),
    ("postgres", "SELECT to_char(created_at, 'YYYY-MM') FROM orders"),
    ("postgres", "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY amount) FROM orders"),
    # lo_ 前缀不能误伤 lower / log
    ("postgres", "SELECT lower(name), log(amount) FROM customers"),
    ("postgres", "SELECT currval('s')"),
    ("postgres", "SELECT a FROM t INTERSECT SELECT a FROM u"),
    ("postgres", "WITH x AS (SELECT * FROM t) SELECT * FROM x"),
    ("mysql", "SELECT DATE_FORMAT(created_at, '%Y-%m') AS m, SUM(amount) FROM orders GROUP BY m"),
    ("mysql", "SELECT `order id` FROM `orders` LIMIT 10"),
    ("mysql", "SELECT IFNULL(a, 0), GROUP_CONCAT(name) FROM t GROUP BY a"),
    ("sqlite", "SELECT strftime('%Y', date), IIF(a > 1, 'y', 'n') FROM t"),
    ("sqlite", "SELECT CAST(a AS REAL) / b FROM t"),
]


@pytest.mark.parametrize("dialect,sql", MUST_ALLOW)
def test_normal_analysis_sql_is_allowed(dialect, sql):
    r = check(sql, dialect=dialect)
    assert r.ok, f"[{dialect}] 正常查询被误拒：{sql}（{r.reason}）"


def test_blacklist_applies_even_when_dialect_is_wrong():
    """dialect 传错（或没传、落到默认的 sqlite）时，别的方言的危险函数照样要挡。"""
    assert not check("SELECT pg_read_file('/etc/passwd')").ok
    assert not check("SELECT LOAD_FILE('/etc/passwd')").ok


def test_ensure_limit_keeps_mysql_backticks():
    """按错的方言重新生成时，`order id` 会变成 "order id"——
    在 MySQL 里那是字符串常量，查询不报错，每行都返回同一个字符串。"""
    out = ensure_limit("SELECT `order id` FROM t", 10, dialect="mysql")
    assert "`order id`" in out and "LIMIT 10" in out


def test_ensure_limit_postgres():
    out = ensure_limit('SELECT "CustomerID" FROM "Orders"', 11, dialect="postgres")
    assert '"CustomerID"' in out and "LIMIT 11" in out


def test_sqlite_sandbox_satisfies_protocol(sales_db):
    sb = SQLiteSandbox(sales_db)
    assert isinstance(sb, Sandbox)
    assert sb.dialect == "sqlite"
