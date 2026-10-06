"""暴露给模型的工具：试跑 SQL、交卷。

只给两个。schema 整份已经在 prompt 里，取数用 `SELECT ... LIMIT` 自己就能办到——
工具由错误数据逼出来，不拍脑袋加。见设计文档第 7 节。
"""

from __future__ import annotations

from llm.base import ToolSpec

EXECUTE_SQL = ToolSpec(
    name="execute_sql",
    description="在数据库上执行一条只读 SELECT 查询，返回结果行。用于验证查询是否正确。",
    parameters={
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "要执行的 SELECT 语句"}},
        "required": ["sql"],
    },
)

SUBMIT_SQL = ToolSpec(
    name="submit_sql",
    description="提交你的最终 SQL 答案，结束本轮任务。",
    parameters={
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "最终的 SELECT 语句"}},
        "required": ["sql"],
    },
)

TOOLS: list[ToolSpec] = [EXECUTE_SQL, SUBMIT_SQL]
