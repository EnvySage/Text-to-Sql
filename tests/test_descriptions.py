"""列说明（ROADMAP 2.1）：每一处对不上都会让某列的说明静默丢失，所以用 BIRD 里真实出现过的脏数据来测。"""

from __future__ import annotations

from agent.schema import Column, Table, render_schema, schema_text
from eval.dataset import _desc_index
from retrieval.descriptions import describe, load_descriptions, normalize

HEADER = "original_column_name,column_name,column_description,data_format,value_description\n"


def _write(path, text, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))


def test_normalize_matches_bird_and_pg_spellings():
    assert normalize("School Code ") == normalize('"School Code"') == "school code"
    assert normalize("`Academic  Year`") == "academic year"


def test_redundant_parts_are_dropped():
    row = {"original_column_name": "cds", "column_name": "CDS", "column_description": "cds ",
           "value_description": "Not useful"}
    assert describe(row) == ""


def test_useful_parts_are_kept_and_cleaned():
    row = {"original_column_name": "A11", "column_name": "average salary",
           "column_description": "average salary of the district",
           "value_description": "commonsense evidence:\n\n0: N;\n1: Y"}
    assert describe(row) == "average salary | average salary of the district | 取值：0: N; 1: Y"


def test_bom_trailing_spaces_and_cp1252_are_handled(tmp_path):
    d = tmp_path / "database_description"
    # 带 BOM 的 UTF-8，列名带尾随空格
    _write(d / "frpm.csv", "﻿" + HEADER + "School Code ,,School identifier,integer,\n", "utf-8")
    # Windows-1252 编码（BIRD 里有 4 个这样的文件），é 在 UTF-8 下解不开
    _write(d / "team.csv", HEADER + 'name,,Équipe name,text,"1: yes"\n', "cp1252")
    # macOS 元数据文件要跳过
    _write(d / "._frpm.csv", "\x00\x05\x16\x07garbage", "latin-1")

    desc = load_descriptions(d)
    assert desc[("frpm", "school code")] == "School identifier"
    assert desc[("team", "name")] == "Équipe name | 取值：1: yes"
    assert len(desc) == 2


def test_render_appends_comment_after_comma():
    tables = [Table(name="orders", columns=[
        Column("id", "INTEGER", True), Column("status", "TEXT", False),
    ])]
    text = render_schema(tables, {("orders", "id"): "订单号", ("orders", "status"): "取值：paid"})
    assert "  id INTEGER PRIMARY KEY, -- 订单号" in text
    assert "  status TEXT -- 取值：paid" in text


def test_render_without_descriptions_is_unchanged():
    tables = [Table(name="t", columns=[Column("a", "INT", False)])]
    assert render_schema(tables) == render_schema(tables, {}) == "CREATE TABLE t (\n  a INT\n);"


def test_pg_quoted_names_still_match():
    tables = [Table(name='"Products"', columns=[Column('"Product Name"', "text", False)])]
    text = render_schema(tables, {("products", "product name"): "商品名"})
    assert '"Product Name" text -- 商品名' in text


def test_schema_text_with_real_sqlite(sales_db):
    text = schema_text(sales_db, notes={("orders", "status"): "取值：paid / refunded / pending"})
    assert "status TEXT -- 取值：paid / refunded / pending" in text
    assert "amount REAL," in text  # 没说明的列不加注释


def test_desc_index_skips_macosx(tmp_path):
    good = tmp_path / "dev_databases" / "shop" / "database_description"
    junk = tmp_path / "__MACOSX" / "dev_databases" / "shop" / "database_description"
    good.mkdir(parents=True); junk.mkdir(parents=True)
    assert _desc_index(tmp_path) == {"shop": good}
