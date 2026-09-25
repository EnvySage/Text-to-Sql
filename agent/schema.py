"""从数据库里抽取 schema，压成适合塞进 prompt 的文本。支持 SQLite 和 PostgreSQL。

baseline 阶段是把整库 schema 全部塞进去。这么做是故意的：先量出
"不做任何检索"的下限，第二周的 schema 裁剪才有一个可对比的基准。
BIRD 里有的库表多列多，整库 schema 会很长——那个长度本身就是要被记录的指标。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from retrieval.descriptions import normalize
from sandbox import dialect_of
from sandbox.postgres import connect_readonly


@dataclass(slots=True)
class Column:
    name: str
    type: str
    pk: bool


@dataclass(slots=True)
class Table:
    name: str
    columns: list[Column]
    sample_rows: list[tuple] = None  # type: ignore[assignment]


def load_schema(db: str | Path, *, sample_rows: int = 0) -> list[Table]:
    """读出所有用户表的结构。``db`` 是 SQLite 文件路径或 ``postgresql://`` 连接地址。

    ``sample_rows`` 大于 0 时附带几行真实数据。列名骗不了人但列值会：
    模型看不到 ``status`` 列里存的是 'paid' 还是 'PAID' 就只能猜。
    baseline 默认不带样例行，同样是为了量出下限。
    """
    if dialect_of(db) == "postgres":
        return _load_schema_postgres(str(db), sample_rows=sample_rows)

    conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables: list[Table] = []
        for name in names:
            cols = [
                Column(name=r[1], type=r[2] or "", pk=bool(r[5]))
                # PRAGMA 在 guard 里是禁止的，但这里是我们自己的内省代码，
                # 不经过 guard——guard 管的是模型生成的 SQL。
                for r in conn.execute(f'PRAGMA table_info("{name}")')
            ]
            rows: list[tuple] = []
            if sample_rows > 0:
                try:
                    rows = [
                        tuple(r)
                        for r in conn.execute(
                            f'SELECT * FROM "{name}" LIMIT {sample_rows}'
                        )
                    ]
                except sqlite3.Error:
                    rows = []
            tables.append(Table(name=name, columns=cols, sample_rows=rows))
        return tables
    finally:
        conn.close()


# 用 pg_catalog 而不是 information_schema：format_type 给出带精度的类型（numeric(10,2)），
# has_table_privilege 只留下当前账号能查的表——看得到查不了的表只会让模型白白报错。
_PG_COLUMNS = """
SELECT quote_ident(c.relname), quote_ident(a.attname),
       format_type(a.atttypid, a.atttypmod),
       COALESCE(a.attnum = ANY (i.indkey), false)
FROM pg_class c
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_index i ON i.indrelid = c.oid AND i.indisprimary
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND NOT c.relispartition
  AND pg_table_is_visible(c.oid)
  AND c.relnamespace NOT IN ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
  AND has_table_privilege(c.oid, 'SELECT')
ORDER BY c.relname, a.attnum
"""


def _load_schema_postgres(dsn: str, *, sample_rows: int) -> list[Table]:
    """PG 的表名、列名存成可以直接写进 SQL 的形式。

    PG 会把不加引号的标识符折叠成小写，BIRD 那种 ``CustomerID``、``Product Name``
    不带引号写就查不到。什么时候需要引号（包括 ``order`` 这类保留字）交给服务端的
    ``quote_ident`` 判断，不自己维护规则。
    """
    conn = connect_readonly(dsn)
    try:
        tables: dict[str, Table] = {}
        for tbl, col, typ, pk in conn.execute(_PG_COLUMNS):
            t = tables.setdefault(tbl, Table(name=tbl, columns=[], sample_rows=[]))
            t.columns.append(Column(name=col, type=typ, pk=pk))
        if sample_rows > 0:
            for t in tables.values():
                try:
                    t.sample_rows = [
                        tuple(r)
                        for r in conn.execute(f"SELECT * FROM {t.name} LIMIT {sample_rows}")
                    ]
                except Exception:
                    # 出错的事务必须回滚，否则后面每张表都报 "current transaction is aborted"。
                    conn.rollback()
                    t.sample_rows = []
        return list(tables.values())
    finally:
        conn.close()


# 2.2 列取值（D18）：取回不超过这么多个不同值就全部列出，否则只给几个样例
ENUM_MAX = 10
SAMPLE_K = 3
VALUE_MAX_CHARS = 40


def _literal(v: Any) -> str:
    """写成 SQL 字面量，模型可以直接照抄：``'M'`` 还是 ``'Male'``，``'1995-03-24'`` 还是 ``950324``。"""
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, bytes):
        return "<二进制>"
    s = str(v)
    if len(s) > VALUE_MAX_CHARS:
        s = s[:VALUE_MAX_CHARS] + "…"
    return "'" + s.replace("'", "''") + "'"


def describe_values(values: list[Any]) -> str:
    if not values:
        return "全为 NULL"
    if len(values) <= ENUM_MAX:
        return "全部取值：" + ", ".join(_literal(v) for v in values)
    return "样例：" + ", ".join(_literal(v) for v in values[:SAMPLE_K])


@lru_cache(maxsize=32)
def column_values(db: str | Path) -> dict[tuple[str, str], str]:
    """``{(表, 列): 取值说明}``，键经过 ``normalize``（ROADMAP 2.2）。

    每列多取一个（``LIMIT ENUM_MAX + 1``），就知道取回的是不是全部取值，
    不用对每一列做全表 ``COUNT(DISTINCT)``。同一个库的每道题都要用，按库缓存。
    """
    tables = load_schema(db)
    if dialect_of(db) == "postgres":
        conn: Any = connect_readonly(str(db))
        quote = lambda name: name  # noqa: E731  PG 的表名列名已经由 quote_ident 按需加过引号
    else:
        conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
        conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
        quote = lambda name: '"' + name.replace('"', '""') + '"'  # noqa: E731
    out: dict[tuple[str, str], str] = {}
    try:
        for t in tables:
            for c in t.columns:
                col = quote(c.name)
                sql = (f"SELECT DISTINCT {col} FROM {quote(t.name)} "
                       f"WHERE {col} IS NOT NULL LIMIT {ENUM_MAX + 1}")
                try:
                    values = [r[0] for r in conn.execute(sql)]
                except Exception:
                    # 取值只是辅助信息，个别列查失败不影响其他列；PG 出错的事务必须回滚
                    conn.rollback()
                    continue
                out[(normalize(t.name), normalize(c.name))] = describe_values(values)
        return out
    finally:
        conn.close()


def merge_notes(*sources: dict[tuple[str, str], str] | None) -> dict[tuple[str, str], str]:
    """把几份列注释（列说明、列取值……）按列合并，同一列用 `` | `` 连起来。"""
    merged: dict[tuple[str, str], list[str]] = {}
    for src in sources:
        for key, text in (src or {}).items():
            if text:
                merged.setdefault(key, []).append(text)
    return {k: " | ".join(v) for k, v in merged.items()}


def render_schema(
    tables: list[Table], notes: dict[tuple[str, str], str] | None = None
) -> str:
    """渲染成 CREATE TABLE 风格的文本。

    用 DDL 而不是自然语言描述：模型在预训练里见过海量 DDL，
    这种格式它最熟，也最省 token。

    ``notes`` 是 ``{(表, 列): 注释}``（键经过 ``normalize``），以行尾注释拼在列后面，
    注释和列紧挨着，模型不用在两段文字之间来回对照。
    """
    blocks: list[str] = []
    for t in tables:
        lines = [f"CREATE TABLE {t.name} ("]
        for i, c in enumerate(t.columns):
            tail = "," if i < len(t.columns) - 1 else ""
            pk = " PRIMARY KEY" if c.pk else ""
            note = (notes or {}).get((normalize(t.name), normalize(c.name)))
            comment = f" -- {note}" if note else ""
            lines.append(f"  {c.name} {c.type}{pk}{tail}{comment}")
        lines.append(");")
        if t.sample_rows:
            head = ", ".join(c.name for c in t.columns)
            lines.append(f"-- 样例行 ({head}):")
            for r in t.sample_rows:
                cells = ", ".join("NULL" if v is None else str(v)[:40] for v in r)
                lines.append(f"--   {cells}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def schema_text(
    db: str | Path,
    *,
    sample_rows: int = 0,
    notes: dict[tuple[str, str], str] | None = None,
) -> str:
    return render_schema(load_schema(db, sample_rows=sample_rows), notes)
