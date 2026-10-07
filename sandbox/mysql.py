"""只读 SQL 执行器（MySQL）。

和 PostgreSQL 一样，服务端库上第二道防线是**账号权限**——会话级的只读能被一条 ``SET``
改回去，账号权限改不回去。guard 之外还有三层：

1. **账号自检**：``SHOW GRANTS`` 里出现写权限或管理权限的，直接拒绝执行；
2. **只读会话**：``SET SESSION TRANSACTION READ ONLY``（改得回去，所以只算补充）；
3. **服务端超时**：``MAX_EXECUTION_TIME``，由数据库自己掐断，不依赖客户端线程。

取行用服务端游标（``SSCursor``）：普通游标在 ``execute`` 时会把整个结果集拉进内存，
``enforce_limit=False`` 时一条没有 LIMIT 的大查询能直接撑爆进程。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlsplit

import pymysql
from pymysql.cursors import SSCursor

from sandbox.base import DEFAULT_MAX_ROWS, DEFAULT_TIMEOUT, BaseSandbox, QueryFailed

# 连不上就尽快失败，别让一道题卡住整轮评测。
CONNECT_TIMEOUT = 10

# 这些词出现在 SHOW GRANTS 里，就说明账号权限超出只读。
# ``USAGE`` 是"没有任何权限"的意思，本身不是权限，所以不在列表里。
_FORBIDDEN = (
    "ALL PRIVILEGES", "SUPER", "FILE", "INSERT", "UPDATE", "DELETE",
    "CREATE", "DROP", "ALTER", "GRANT OPTION", "REFERENCES", "INDEX",
    "LOCK TABLES", "EXECUTE", "PROCESS", "RELOAD", "SHUTDOWN", "REPLICATION",
)

# 服务端中断查询时的错误码。
_ER_QUERY_TIMEOUT = 3024


class UnsafeAccount(Exception):
    """连接账号的权限超出只读，拒绝在上面执行模型生成的 SQL。"""


def parse_dsn(dsn: str) -> dict[str, Any]:
    """把 ``mysql://用户:密码@主机:端口/库`` 拆成 pymysql 的连接参数。

    用户名和密码要做 percent 解码——密码里带 ``@`` ``/`` ``:`` 时只能这么写。
    """
    u = urlsplit(dsn)
    return {
        "host": u.hostname or "127.0.0.1",
        "port": u.port or 3306,
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "database": (u.path or "/").lstrip("/") or None,
        "charset": "utf8mb4",
    }


def connect_readonly(
    dsn: str, *, timeout: float = DEFAULT_TIMEOUT, verify_account: bool = True
) -> pymysql.connections.Connection:
    """打开一条只读、带服务端超时的连接。

    ``verify_account=False`` 只给测试用：单独验证只读会话这一层时，需要一个有写权限的账号，
    证明账号层失守时会话层仍然挡得住。
    """
    try:
        conn = pymysql.connect(**parse_dsn(dsn), connect_timeout=CONNECT_TIMEOUT,
                               autocommit=True)
    except pymysql.Error as exc:
        raise QueryFailed(f"连接数据库失败：{exc}") from exc

    try:
        with conn.cursor() as cur:
            # 只读会话：能被改回去，所以只算补充，真正的防线是账号权限。
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            try:
                # MariaDB 没有这个变量，5.7 以下也没有。拿不到就算了，别因此连不上。
                cur.execute("SET SESSION MAX_EXECUTION_TIME = %s",
                            (max(int(timeout * 1000), 1),))
            except pymysql.Error:
                pass
        if verify_account:
            _verify_account(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _verify_account(conn: pymysql.connections.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("SHOW GRANTS")
        grants = " ".join(str(r[0]) for r in cur.fetchall()).upper()
    hit = [w for w in _FORBIDDEN if w in grants]
    if hit:
        raise UnsafeAccount(
            f"连接账号有写权限或管理权限（{'、'.join(hit)}），拒绝执行。"
            "请换成只授予 SELECT 权限的账号，见 docs/DESIGN.md 3.2。"
        )


def _describe(exc: pymysql.Error) -> str:
    code = exc.args[0] if exc.args else None
    if code == _ER_QUERY_TIMEOUT:
        return "查询超时（被服务端中断）"
    return str(exc)


class MySQLSandbox(BaseSandbox):
    dialect = "mysql"

    def __init__(
        self,
        dsn: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        super().__init__(timeout=timeout, max_rows=max_rows)
        self.dsn = dsn

    def _fetch(self, sql: str, n: int) -> tuple[list[str], list[tuple[Any, ...]]]:
        try:
            conn = connect_readonly(self.dsn, timeout=self.timeout)
        except UnsafeAccount as exc:
            raise QueryFailed(str(exc)) from exc

        try:
            with conn.cursor(SSCursor) as cur:
                cur.execute(sql)
                columns = [d[0] for d in (cur.description or [])]
                return columns, [tuple(r) for r in cur.fetchmany(n)]
        except pymysql.Error as exc:
            raise QueryFailed(_describe(exc)) from exc
        finally:
            conn.close()
