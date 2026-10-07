"""2.5 schema 裁剪：大库只给表名清单，模型用 get_schema 按需取。

实测 xingchengwms（148 张表）：完整 DDL 6 万字符 / 单题输入 3.2 万 token；
只给表名清单后 schema 2.9K 字符 / 单题输入 8.7K token——**省 73%**。
"""

from __future__ import annotations

from agent.schema import schema_index, table_ddl


def test_index_lists_names_but_not_types(sales_db):
    idx = schema_index(sales_db)
    assert "customers" in idx and "orders" in idx
    assert "TEXT" not in idx          # 只给表名，不给类型


def test_index_tells_the_model_to_use_the_tool(sales_db):
    """光给表名清单，模型不知道还能取详细结构——得在文本里说。"""
    assert "get_schema" in schema_index(sales_db)


def test_index_is_much_shorter_than_the_full_schema(sales_db):
    from agent.schema import schema_text

    assert len(schema_index(sales_db)) < len(schema_text(sales_db))


def test_table_ddl_returns_only_that_table(sales_db):
    ddl = table_ddl(sales_db, "orders")
    assert "CREATE TABLE orders" in ddl
    assert "CREATE TABLE customers" not in ddl


def test_table_ddl_matches_case_insensitively(sales_db):
    """模型把表名大小写写错是常事。"""
    assert "CREATE TABLE orders" in table_ddl(sales_db, "ORDERS")


def test_table_ddl_unknown_name_lists_candidates(sales_db):
    """拼错名字时给候选，别只回一句"没有这张表"——那样它只能瞎猜。"""
    out = table_ddl(sales_db, "nope")
    assert "没有名为" in out
    assert "orders" in out and "customers" in out
