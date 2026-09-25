"""只读 SQL 执行。上层用 ``open_sandbox(连接目标)`` 拿到执行器，不关心背后是哪种库。"""

from __future__ import annotations

from pathlib import Path

from sandbox.base import Sandbox
from sandbox.executor import SQLiteSandbox
from sandbox.postgres import PostgresSandbox

_PG_SCHEMES = ("postgresql://", "postgres://")
_MYSQL_SCHEMES = ("mysql://",)


def dialect_of(target: str | Path) -> str:
    """连接目标 → sqlglot 方言名。不是 URL 的一律当 SQLite 文件路径。"""
    s = str(target)
    if s.startswith(_PG_SCHEMES):
        return "postgres"
    if s.startswith(_MYSQL_SCHEMES):
        return "mysql"
    return "sqlite"


def open_sandbox(target: str | Path, **kwargs) -> Sandbox:
    dialect = dialect_of(target)
    if dialect == "postgres":
        return PostgresSandbox(str(target), **kwargs)
    if dialect == "mysql":
        # 不能退化成当文件路径处理：那样报的是"数据库不存在"，排查方向会被带偏。
        raise NotImplementedError("MySQL 执行器尚未实现，见 docs/ROADMAP.md M.3")
    return SQLiteSandbox(target, **kwargs)
