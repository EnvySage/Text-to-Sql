"""MySQL 执行器：连接地址解析、账号自检、连不上时的行为。

不需要 MySQL 服务端就能测的部分——账号自检用假的连接对象验。
"""

from __future__ import annotations

import pytest

from agent.schema import _quote_mysql_ident
from sandbox.mysql import (
    MySQLSandbox,
    UnsafeAccount,
    _verify_account,
    parse_dsn,
)


# ── 连接地址 ──────────────────────────────────────────────────────────────

def test_parse_dsn():
    p = parse_dsn("mysql://user:pass@db.example.com:3307/shop")
    assert (p["host"], p["port"], p["user"], p["password"], p["database"]) == (
        "db.example.com", 3307, "user", "pass", "shop",
    )


def test_parse_dsn_percent_decodes_password():
    """密码里带 @ / : 只能 percent 编码写，得解回来。"""
    assert parse_dsn("mysql://u:p%40ss%2Fword@127.0.0.1:3306/db")["password"] == "p@ss/word"


def test_parse_dsn_defaults():
    p = parse_dsn("mysql://u@127.0.0.1/db")
    assert p["port"] == 3306 and p["database"] == "db" and p["charset"] == "utf8mb4"


# ── 标识符引号 ────────────────────────────────────────────────────────────

def test_quote_ident_uses_backticks():
    """列名里空格和保留字（order / key / desc）很常见，不加引号写不出合法 SQL。"""
    assert _quote_mysql_ident("order") == "`order`"


def test_quote_ident_escapes_embedded_backticks():
    assert _quote_mysql_ident("we`ird") == "`we``ird`"


# ── 账号自检 ──────────────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, sql: str, *args: object) -> None:
        self.sql = sql

    def fetchall(self) -> list[tuple]:
        return self._rows

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeConn:
    """只实现 ``cursor()``——账号自检只用得到这一个方法。"""

    def __init__(self, grants: list[tuple]) -> None:
        self._grants = grants

    def cursor(self, *args: object, **kwargs: object) -> _FakeCursor:
        return _FakeCursor(self._grants)


def test_readonly_account_passes():
    _verify_account(_FakeConn([
        ("GRANT USAGE ON *.* TO 'ro'@'%'",),          # USAGE = 没有任何权限，不是权限
        ("GRANT SELECT ON `shop`.* TO 'ro'@'%'",),
    ]))


@pytest.mark.parametrize("grant", [
    "GRANT ALL PRIVILEGES ON *.* TO 'x'@'%'",
    "GRANT INSERT ON `shop`.* TO 'x'@'%'",
    "GRANT UPDATE ON `shop`.* TO 'x'@'%'",
    "GRANT DELETE ON `shop`.* TO 'x'@'%'",
    "GRANT SUPER ON *.* TO 'x'@'%'",
    "GRANT FILE ON *.* TO 'x'@'%'",
    "GRANT SELECT, INSERT ON `shop`.* TO 'x'@'%'",
    "GRANT CREATE ON `shop`.* TO 'x'@'%'",
])
def test_write_or_admin_account_is_rejected(grant: str):
    with pytest.raises(UnsafeAccount):
        _verify_account(_FakeConn([(grant,)]))


# ── 连不上时 ──────────────────────────────────────────────────────────────

def test_connection_failure_returns_error_not_raises():
    """连不上是一次普通失败，不能把评测主循环炸掉。"""
    r = MySQLSandbox("mysql://u:p@127.0.0.1:1/db").run("SELECT 1")
    assert not r.ok and "连接数据库失败" in r.error
