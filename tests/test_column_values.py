"""2.2 列取值：模型会照抄这里的字面量写 WHERE 条件，格式、引号、"是不是全部"都不能错。"""

from __future__ import annotations

import sqlite3

import pytest

from agent.schema import (
    ENUM_MAX, VALUE_MAX_CHARS, column_values, describe_values, merge_notes, schema_text,
)


@pytest.fixture()
def values_db(tmp_path):
    path = tmp_path / "v.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (exact10 INT, eleven INT, allnull TEXT, long TEXT, quote TEXT)")
    rows = []
    for i in range(20):
        rows.append((
            i % ENUM_MAX,          # 恰好 10 种：必须判为"全部取值"
            i % (ENUM_MAX + 1),    # 11 种：只能给样例
            None,
            "x" * 100,
            "O'Brien",
        ))
    conn.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit(); conn.close()
    return path


def test_enum_boundary_is_exact(values_db):
    v = column_values(values_db)
    assert v[("t", "exact10")].startswith("全部取值：")
    assert v[("t", "exact10")].count(",") == ENUM_MAX - 1
    assert v[("t", "eleven")].startswith("样例：")
    assert v[("t", "eleven")].count(",") == 2


def test_null_long_and_quotes(values_db):
    v = column_values(values_db)
    assert v[("t", "allnull")] == "全为 NULL"
    assert v[("t", "long")] == "全部取值：'" + "x" * VALUE_MAX_CHARS + "…'"
    assert v[("t", "quote")] == "全部取值：'O''Brien'"


def test_literals_distinguish_numbers_from_strings():
    assert describe_values([1, 2.5]) == "全部取值：1, 2.5"
    assert describe_values(["1", "2"]) == "全部取值：'1', '2'"


def test_real_db_values_are_rendered(sales_db):
    notes = merge_notes({("orders", "status"): "订单状态"}, column_values(sales_db))
    text = schema_text(sales_db, notes=notes)
    assert "status TEXT -- 订单状态 | 全部取值：'paid', 'refunded', 'pending'" in text
    # NULL 不算一种取值
    assert "amount REAL, -- 全部取值：100.0, 50.5, 200.0" in text


def test_merge_notes_skips_missing():
    assert merge_notes(None, {("t", "a"): "x"}, {("t", "a"): ""}) == {("t", "a"): "x"}
