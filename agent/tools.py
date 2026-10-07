"""暴露给模型的工具：试跑 SQL、交卷。

只给两个。schema 整份已经在 prompt 里，取数用 `SELECT ... LIMIT` 自己就能办到——
工具由错误数据逼出来，不拍脑袋加。为什么只给两个见
`docs/superpowers/specs/2026-10-06-agent-loop-design.md` 第 7 节。
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

ASK_USER = ToolSpec(
    name="ask_user",
    description=(
        "当问题里的**业务名词**含义不明确、而且不同解释会写出不同的 SQL 时，问用户。"
        "一次只问一个词。**不要问表结构**——那可以用 execute_sql 自己查。"
        "**必须给候选**，用户点一个就行。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "term": {"type": "string", "description": "不明确的业务名词，如「复购」"},
            "question": {"type": "string", "description": "要问用户的具体问题"},
            "candidates": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-3 个候选解释，覆盖最可能的理解",
            },
        },
        "required": ["term", "question", "candidates"],
    },
)

# 默认工具集。``ask_user`` 不在里面——它要有真人在场才有意义，
# 由 ``core.run(ask=...)`` 决定要不要加上去（见那个函数的说明）。
TOOLS: list[ToolSpec] = [EXECUTE_SQL, SUBMIT_SQL]
