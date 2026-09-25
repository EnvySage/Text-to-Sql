"""baseline：一次调用，不给工具，不看执行结果，不重试。

这是整个项目的对照组。它故意什么都不做——没有 schema 裁剪、没有 few-shot、
没有执行反馈。后面每加一个模块，都要和这条线比，比不过就说明那个模块是摆设。

先量出下限，再谈优化。顺序反了的话，你永远说不清提升是从哪来的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from llm.base import LLMProvider, Message, Usage

SYSTEM = """你是一个 SQLite 专家。根据给定的数据库结构，把用户的问题翻译成一条 SQL 查询。

要求：
- 只输出一条 SELECT 语句，不要输出任何解释文字。
- 用 ```sql 代码块包裹。
- 只使用结构中确实存在的表名和列名。
- 列名包含空格或特殊字符时用双引号包裹。"""

USER_TEMPLATE = """数据库结构：

{schema}
{evidence}
问题：{question}

输出 SQL："""

# 模型经常把 SQL 包在代码块里，也经常不包。两种都要能取出来。
_FENCE = re.compile(r"```(?:sql)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)


def extract_sql(text: str | None) -> str:
    """从模型回复里取出 SQL。

    取不到就返回空串，由调用方记成一次失败——绝不能猜，
    猜出来的 SQL 会污染准确率。
    """
    if not text:
        return ""
    m = _FENCE.search(text)
    candidate = (m.group(1) if m else text).strip()
    # 去掉可能残留的前缀说明，只从第一个 SELECT/WITH 开始取
    m2 = re.search(r"\b(SELECT|WITH)\b", candidate, re.IGNORECASE)
    if m2:
        candidate = candidate[m2.start():]
    return candidate.strip().rstrip(";").strip()


@dataclass(slots=True)
class GenResult:
    sql: str
    usage: Usage
    raw_text: str = ""
    error: str = ""


def generate_sql(
    provider: LLMProvider,
    *,
    schema: str,
    question: str,
    evidence: str = "",
    max_tokens: int = 2048,
) -> GenResult:
    """单次生成。不带工具，不看结果，不重试。"""
    ev = f"\n业务口径说明：{evidence}\n" if evidence else ""
    prompt = USER_TEMPLATE.format(schema=schema, evidence=ev, question=question)

    resp = provider.chat(
        system=SYSTEM,
        messages=[Message.user(prompt)],
        tools=None,
        max_tokens=max_tokens,
    )
    sql = extract_sql(resp.text)
    return GenResult(
        sql=sql,
        usage=resp.usage,
        raw_text=resp.text or "",
        error="" if sql else "模型回复里没有提取到 SQL",
    )
