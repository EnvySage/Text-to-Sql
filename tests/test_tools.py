"""工具定义：只给两个，别的一律不加。"""

from __future__ import annotations

from agent.tools import EXECUTE_SQL, SUBMIT_SQL, TOOLS


def test_exactly_two_tools():
    assert [t.name for t in TOOLS] == ["execute_sql", "submit_sql"]


def test_both_require_sql_arg():
    for t in (EXECUTE_SQL, SUBMIT_SQL):
        assert t.parameters["type"] == "object"   # 厂商侧要求 JSON Schema 顶层是 object
        assert t.parameters["required"] == ["sql"]
        assert "sql" in t.parameters["properties"]


def test_descriptions_are_nonempty():
    for t in TOOLS:
        assert t.description.strip()
