"""few-shot 版 baseline：在 prompt 里插入几条相似问题的标准 SQL。

和 ``baseline_dialect`` 一样，system prompt 只把方言名换掉，其余全部复用 ``baseline``——
这样和 baseline 的差异只有"prompt 里有没有示例"这一个变量，别的措辞一字未动。

示例插在 ``USER_TEMPLATE`` 的 evidence 槽位（schema 之后、问题之前）：既贴着问题，
又不必另写一份模板。
"""

from __future__ import annotations

from agent import baseline, baseline_dialect
from agent.baseline import GenResult
from llm.base import LLMProvider, Message
from retrieval.fewshot import Example, format_examples


def generate_sql(
    provider: LLMProvider,
    *,
    dialect: str,
    schema: str,
    question: str,
    evidence: str = "",
    examples: list[Example] | None = None,
    max_tokens: int = 2048,
) -> GenResult:
    """单次生成，prompt 里带 few-shot 示例。不带工具，不看结果，不重试。"""
    ev = f"\n业务口径说明：{evidence}\n" if evidence else ""
    block = format_examples(examples or [])
    prefix = f"\n{block}" if block else ""

    prompt = baseline.USER_TEMPLATE.format(
        schema=schema, evidence=prefix + ev, question=question
    )
    resp = provider.chat(
        system=baseline_dialect.system_prompt(dialect),
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
