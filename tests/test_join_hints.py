"""join 提示：只留像标识符的同名列，滤掉 amount / type 这类假连接键。

假连接键比没有更糟——模型本来连得对，喂错的反而带偏。
"""

from __future__ import annotations

from agent.schema import Column, Table, _join_hints, render_schema


def _t(name: str, *cols: str) -> Table:
    return Table(name=name, columns=[Column(name=c, type="TEXT", pk=False) for c in cols])


def test_shared_identifier_columns_are_listed():
    h = _join_hints([_t("a", "user_id", "amount"), _t("b", "user_id", "amount")])
    assert "user_id" in h


def test_non_identifier_shared_columns_are_dropped():
    """amount 在三张表里都有，但它不是连接键。"""
    h = _join_hints([_t("a", "amount", "type"), _t("b", "amount", "type")])
    assert h == ""


def test_no_shared_columns_returns_empty():
    assert _join_hints([_t("a", "x"), _t("b", "y")]) == ""


def test_single_table_returns_empty():
    assert _join_hints([_t("a", "user_id")]) == ""


def test_code_and_uuid_and_key_suffixes_are_kept():
    h = _join_hints([_t("a", "setCode", "uuid", "fk_key"), _t("b", "setCode", "uuid", "fk_key")])
    for col in ("setCode", "uuid", "fk_key"):
        assert col in h


def test_render_schema_default_off():
    """默认不附连接提示——打开会改变 schema 文本，已有数字就不可比了。"""
    tables = [_t("a", "user_id"), _t("b", "user_id")]
    assert "可连接列" not in render_schema(tables)
    assert "可连接列" in render_schema(tables, join_hints=True)
