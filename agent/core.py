"""agent 主循环：给模型工具，让它试跑 SQL、自己决定何时交卷。

循环只产出事件流，不做渲染。runner 用 ``consume()`` 收敛成结果对象。
2.4 的靶子很小（SQL 跑不通 + 没吐出 SQL 约 5 题），价值在架构：planner / verifier /
CLI / Web 将来都挂在这套事件流上。见 docs/DESIGN.md 4.4。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from agent import baseline
from agent.events import AgentEvent
from agent.tools import TOOLS
from llm.base import LLMError, LLMProvider, Message, ToolResult, Usage
from sandbox.base import Sandbox

# 防跑飞的兜底，不是预算：正常模型 1-3 步交卷，撞到它说明模型绕不出来。
MAX_STEPS_DEFAULT = 10

SYSTEM = baseline.SYSTEM + """

你可以调用以下工具：
- execute_sql：试跑一条查询，看真实结果，用来验证你的 SQL 是否正确。
- submit_sql：确定之后，用它提交最终答案。
不确定时先 execute_sql 验证，确认无误再用 submit_sql 交卷。"""


@dataclass(slots=True)
class AgentOutcome:
    """一次 agent 运行的结果，供 runner 使用。"""

    sql: str = ""
    steps: int = 0
    tool_calls: int = 0
    hit_cap: bool = False
    usage: Usage = field(default_factory=Usage)
    error: str = ""


def run(
    question: str,
    *,
    provider: LLMProvider,
    sandbox: Sandbox,
    schema: str,
    evidence: str = "",
    max_steps: int = MAX_STEPS_DEFAULT,
    max_tokens: int = 8192,
) -> Iterator[AgentEvent]:
    """跑一轮工具循环，产出事件流。最后必是一个 ``final`` 或 ``error``。"""
    ev = f"\n业务口径说明：{evidence}\n" if evidence else ""
    messages = [Message.user(baseline.USER_TEMPLATE.format(
        schema=schema, evidence=ev, question=question))]
    usage = Usage()
    n_calls = 0

    for step in range(1, max_steps + 1):
        yield AgentEvent("step_start", {"step": step})
        try:
            resp = provider.chat(
                system=SYSTEM, messages=messages, tools=TOOLS, max_tokens=max_tokens,
            )
        except LLMError as exc:
            yield AgentEvent("error", {"message": str(exc)},
                             usage=usage)
            return
        usage = usage + resp.usage

        if not resp.tool_calls:
            # 模型没调工具就停了：走兜底，从文字里抠（和单次路径同源）。
            yield AgentEvent("final", {
                "sql": baseline.extract_sql(resp.text),
                "steps": step, "tool_calls": n_calls, "hit_cap": False,
            }, usage=usage)
            return

        messages.append(resp.to_message())
        results: list[ToolResult] = []
        for tc in resp.tool_calls:
            n_calls += 1
            if tc.name == "submit_sql":
                yield AgentEvent("final", {
                    "sql": str(tc.args.get("sql", "")),
                    "steps": step, "tool_calls": n_calls, "hit_cap": False,
                }, usage=usage)
                return
            if tc.name != "execute_sql":
                results.append(ToolResult(
                    call_id=tc.id, content=f"未知工具：{tc.name}", is_error=True))
                continue
            # 先产出 tool_call 再执行：消费者要在结果之前看到这次调用。
            yield AgentEvent("tool_call", {"name": tc.name, "args": tc.args})
            res = sandbox.run(str(tc.args.get("sql", "")))
            yield AgentEvent("tool_result", {
                "name": tc.name, "ok": res.ok, "rows": len(res.rows), "error": res.error,
            })
            # to_markdown 只给前 20 行：结果集最多 2000 行，全塞进 context 太贵。
            results.append(ToolResult(
                call_id=tc.id, content=res.to_markdown(), is_error=not res.ok))
        messages.append(Message.results(results))

    yield AgentEvent("final", {
        "sql": "", "steps": max_steps, "tool_calls": n_calls, "hit_cap": True,
    }, usage=usage)


def consume(events: Iterator[AgentEvent]) -> AgentOutcome:
    """把事件流收敛成结果对象。最后一个 ``final`` / ``error`` 决定结果。"""
    out = AgentOutcome()
    for e in events:
        if e.type in ("final", "error"):
            p = e.payload
            out.sql = p.get("sql", "")
            out.error = p.get("message", "")
            out.steps = p.get("steps", 0)
            out.tool_calls = p.get("tool_calls", 0)
            out.hit_cap = p.get("hit_cap", False)
            out.usage = e.usage or Usage()
    return out
