"""baseline 的方言版：system prompt 只把"SQLite"换成目标方言名，其余一字不改。

``baseline.py`` 是冻结的对照组（见 DESIGN 3.3），不能改它的 prompt。跨方言对比要求只有方言
这一个变量，所以这里直接复用它的 SYSTEM、USER_TEMPLATE 和 extract_sql，而不是另写一份 prompt——
另写的话，措辞上的任何差异都会混进"方言"的影响里。
"""

from __future__ import annotations

from agent import baseline
from agent.baseline import GenResult
from llm.base import LLMProvider, Message

DIALECT_NAMES = {"sqlite": "SQLite", "postgres": "PostgreSQL", "mysql": "MySQL"}


def system_prompt(dialect: str) -> str:
    return baseline.SYSTEM.replace("SQLite", DIALECT_NAMES[dialect])


def generate_sql(
    provider: LLMProvider,
    *,
    dialect: str,
    schema: str,
    question: str,
    evidence: str = "",
    max_tokens: int = 2048,
) -> GenResult:
    """单次生成。SQLite 时原样调用 baseline，保证已有的 SQLite 评测行为不变。"""
    if dialect == "sqlite":
        return baseline.generate_sql(
            provider, schema=schema, question=question,
            evidence=evidence, max_tokens=max_tokens,
        )

    ev = f"\n业务口径说明：{evidence}\n" if evidence else ""
    prompt = baseline.USER_TEMPLATE.format(schema=schema, evidence=ev, question=question)
    resp = provider.chat(
        system=system_prompt(dialect),
        messages=[Message.user(prompt)],
        tools=None,
        max_tokens=max_tokens,
    )
    sql = baseline.extract_sql(resp.text)
    return GenResult(
        sql=sql,
        usage=resp.usage,
        raw_text=resp.text or "",
        error="" if sql else "模型回复里没有提取到 SQL",
    )
