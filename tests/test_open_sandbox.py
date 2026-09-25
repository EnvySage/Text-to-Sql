"""按连接目标选执行器。不需要任何数据库在线。"""

from __future__ import annotations

import pytest

from sandbox import dialect_of, open_sandbox
from sandbox.executor import SQLiteSandbox
from sandbox.postgres import PostgresSandbox


@pytest.mark.parametrize("target,dialect", [
    ("eval/datasets/mini/shop.sqlite", "sqlite"),
    ("D:/data/x.db", "sqlite"),
    ("postgresql://u:p@localhost:5432/db", "postgres"),
    ("postgres://u:p@localhost/db", "postgres"),
    ("mysql://u:p@localhost/db", "mysql"),
])
def test_dialect_of(target, dialect):
    assert dialect_of(target) == dialect


def test_open_sandbox_picks_implementation(sales_db):
    assert isinstance(open_sandbox(sales_db), SQLiteSandbox)
    assert isinstance(open_sandbox("postgresql://u:p@localhost/db"), PostgresSandbox)


def test_open_sandbox_passes_limits_through(sales_db):
    sb = open_sandbox(sales_db, max_rows=7, timeout=3)
    assert sb.max_rows == 7 and sb.timeout == 3


def test_mysql_is_not_silently_treated_as_file():
    with pytest.raises(NotImplementedError, match="MySQL"):
        open_sandbox("mysql://u:p@localhost/db")


def test_connection_failure_returns_error_not_raises():
    """连不上是一次普通失败，不能把评测主循环炸掉。

    用非法端口号让 libpq 立刻失败：Windows 上连本机关闭的端口不会马上被拒，要等满连接超时。
    """
    r = PostgresSandbox("postgresql://u:p@127.0.0.1:99999/db").run("SELECT 1")
    assert not r.ok and "连接数据库失败" in r.error


def test_with_search_path_builds_url():
    from urllib.parse import parse_qs, urlsplit

    from sandbox.postgres import with_search_path

    dsn = with_search_path("postgresql://u:p@h:5432/bird", "formula_1")
    assert dialect_of(dsn) == "postgres"
    assert parse_qs(urlsplit(dsn).query)["options"] == ["-c search_path=formula_1"]
    # 已有的 options 保留
    dsn2 = with_search_path("postgresql://u:p@h/db?options=-c%20work_mem%3D64MB", "x")
    assert parse_qs(urlsplit(dsn2).query)["options"] == ["-c work_mem=64MB -c search_path=x"]
