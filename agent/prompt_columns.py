"""2.3 输出列约束：在方言版 prompt 上再叠一条"只返回问题问到的列"的硬规则。

``baseline.py`` 冻结（它是所有消融的对照组），``baseline_dialect.py`` 的 PG 数字
（47.6%）已验证，所以这里另起一份，不去改那两个模块——代价是重复 8 行调用代码，
换的是已验证行为零风险。

和 baseline_dialect 一样复用 ``baseline`` 的 USER_TEMPLATE / extract_sql / GenResult，
保证除了 system prompt，请求的其余部分逐字相同。这样 2.3 和 colvalues 的差异
只可能来自这一条规则。
"""

from __future__ import annotations

from agent import baseline, baseline_dialect
from agent.baseline import GenResult
from llm.base import LLMProvider, Message

# 实验的唯一变量：这条规则的有无。改措辞就是另一个实验，改完要重跑对照。
#
# 措辞对准 outcols 里 12 道列数错的常见模式（9/12）：
#   多带辅助列（问"哪些 bond 的 atom id"却多返回 atom_id2、问"哪场比赛"却多带 race name），
#   以及把问题问到的多项拼成一列（问 full name and email 却把姓名拼成 full_name 一列）。
EXTRA_RULE = """- 只输出问题问到的列，不多也不少。
- 不要附带问题没问到的列，尤其是用于定位、排序、筛选的 id、主键、名称、分数。
- 问题问到的每一项信息各占一列，不要合并（如姓和名要分两列，不要拼成一个全名）。"""


def system_prompt(dialect: str) -> str:
    """方言版 prompt 原文，末尾追加输出列规则。"""
    return baseline_dialect.system_prompt(dialect) + "\n" + EXTRA_RULE


def generate_sql(
    provider: LLMProvider,
    *,
    dialect: str,
    schema: str,
    question: str,
    evidence: str = "",
    max_tokens: int = 2048,
) -> GenResult:
    """单次生成，system prompt 带输出列约束。不带工具，不看结果，不重试。"""
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
