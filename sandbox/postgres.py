"""只读 SQL 执行器（PostgreSQL）。

SQLite 的 ``mode=ro`` 在打开文件时就定死了；服务端库上与之同等强度的，是账号本身没有写权限。
guard 之外，这里还有四层，各挡各的：

1. **账号自检**：连上后先查当前账号，能改数据、是超级用户、或能碰服务器文件的，直接拒绝执行。
   只读事务可以被 ``SET TRANSACTION READ WRITE`` 改回去（实测），账号权限改不回去；
2. **只读事务**：会话默认只读，psycopg 每个事务再显式 ``BEGIN READ ONLY``；
3. **服务端游标**：``DECLARE CURSOR`` 只接受 SELECT / VALUES，写语句在语法层就进不来；
4. **服务端超时**：``statement_timeout`` 由数据库自己掐断，不依赖客户端线程。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg import errors
from psycopg.conninfo import conninfo_to_dict

from sandbox.base import DEFAULT_MAX_ROWS, DEFAULT_TIMEOUT, BaseSandbox, QueryFailed

# 连不上就尽快失败，别让一道题卡住整轮评测。
CONNECT_TIMEOUT = 10

# 任意一张用户表可写、是超级用户、或属于能读写服务器文件/执行程序的内置角色，都不合格。
_ACCOUNT_CHECK = """
SELECT
  (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
  EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'v', 'f')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')
  ),
  EXISTS (
    SELECT 1 FROM pg_roles r
    WHERE r.rolname IN ('pg_read_server_files', 'pg_write_server_files',
                        'pg_execute_server_program')
      AND pg_has_role(current_user, r.oid, 'MEMBER')
  )
"""


class UnsafeAccount(Exception):
    """连接账号的权限超出只读，拒绝在上面执行模型生成的 SQL。"""


def connect_readonly(
    dsn: str, *, timeout: float = DEFAULT_TIMEOUT, verify_account: bool = True
) -> psycopg.Connection:
    """打开一条只读、带服务端超时的连接。

    ``verify_account=False`` 只给测试用：单独验证只读事务这一层时，需要一个有写权限的账号，
    证明账号层失守时事务层仍然挡得住。
    """
    # 在会话建立时由服务端设置，不经过任何一条可能被模型影响的 SQL。
    # 连接地址里可能已经带了 options（比如 search_path），要合并而不是覆盖；
    # 我们的设置放在最后，同名参数以最后一个为准，地址里写了只读 off 也会被压回去。
    #
    # cursor_tuple_fraction=1：服务端游标默认按"只取前 10% 的行"选计划，和直接执行的计划不同，
    # 遇到并列值或没有 ORDER BY 时返回的行也不同（实测 BIRD 标准 SQL 有多条如此）。
    # 游标在这里只负责限制取回的行数，不应该改变结果。
    own = (
        "-c default_transaction_read_only=on "
        f"-c statement_timeout={max(int(timeout * 1000), 1)} "
        "-c cursor_tuple_fraction=1"
    )
    existing = conninfo_to_dict(dsn).get("options") or ""
    conn = psycopg.connect(
        dsn,
        connect_timeout=CONNECT_TIMEOUT,
        options=f"{existing} {own}".strip(),
    )
    conn.read_only = True
    if verify_account:
        try:
            _verify_account(conn)
        except BaseException:
            conn.close()
            raise
    return conn


def with_search_path(dsn: str, schema: str) -> str:
    """给 ``postgresql://`` 连接地址加上 ``search_path``，SQL 里不带 schema 前缀就能查到这个 schema 的表。

    评测时 11 个库在同一个 PG 库的不同 schema 里，每道题靠它只看自己的库。
    """
    parts = urlsplit(dsn)
    query = parse_qsl(parts.query)
    options = " ".join(v for k, v in query if k == "options")
    options = f"{options} -c search_path={schema}".strip()
    query = [(k, v) for k, v in query if k != "options"] + [("options", options)]
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))


def _verify_account(conn: psycopg.Connection) -> None:
    is_super, can_write, server_files = conn.execute(_ACCOUNT_CHECK).fetchone()
    conn.rollback()
    problems = []
    if is_super:
        problems.append("是超级用户")
    if can_write:
        problems.append("对部分表有写权限")
    if server_files:
        problems.append("能读写服务器文件或执行程序")
    if problems:
        raise UnsafeAccount(
            f"连接账号{'、'.join(problems)}，拒绝执行。"
            "请换成只授予 SELECT 权限的账号，见 docs/DESIGN.md 3.2。"
        )


def _describe(exc: psycopg.Error) -> str:
    """给模型看的报错：主信息 + 提示。

    不用 ``str(exc)``：服务端游标会把 SQL 包成 ``DECLARE "..." CURSOR FOR ...``，
    报错里的 LINE 片段带着这层包装，模型会以为是自己写出来的。
    """
    diag = exc.diag
    msg = diag.message_primary or str(exc).splitlines()[0]
    if diag.message_hint:
        msg += f"\nHINT: {diag.message_hint}"
    return msg


class PostgresSandbox(BaseSandbox):
    dialect = "postgres"

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
        except psycopg.Error as exc:
            raise QueryFailed(f"连接数据库失败：{str(exc).strip()}") from exc

        try:
            # 服务端游标只把需要的 n 行传回来。普通游标在 execute 时会把整个结果集拉进内存，
            # enforce_limit=False 时一条没有 LIMIT 的大查询能直接撑爆进程。
            with conn.cursor(name="agent_query") as cur:
                cur.execute(sql)
                columns = [d.name for d in (cur.description or [])]
                return columns, cur.fetchmany(n)
        except errors.QueryCanceled as exc:
            raise QueryFailed(f"查询超时（超过 {self.timeout:.0f} 秒被中断）") from exc
        except psycopg.Error as exc:
            raise QueryFailed(_describe(exc)) from exc
        finally:
            conn.close()
