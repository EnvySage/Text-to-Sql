"""agent 主循环：给模型工具，让它试跑 SQL、自己决定何时交卷。

循环只产出事件流，不做渲染。runner 用 ``consume()`` 收敛成结果对象。
2.4 的靶子很小（SQL 跑不通 + 没吐出 SQL 约 5 题），价值在架构：planner / verifier /
CLI / Web 将来都挂在这套事件流上。见 docs/DESIGN.md 4.4。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from agent import baseline, baseline_dialect
from agent.events import AgentEvent
from agent.tools import TOOLS
from llm.base import LLMError, LLMProvider, Message, ToolResult, Usage
from sandbox.base import Sandbox

# 防跑飞的兜底，不是预算：正常模型 1-3 步交卷，撞到它说明模型绕不出来。
MAX_STEPS_DEFAULT = 10

# 工具说明。拼在方言版 baseline prompt 后面，不是替掉它——
# 跨方言对比要求只变方言这一个变量，工具说明对各方言必须逐字相同。
TOOL_SUFFIX = """

你可以调用以下工具：
- execute_sql：试跑一条查询，看真实结果，用来验证你的 SQL 是否正确。
- submit_sql：确定之后，用它提交最终答案。
不确定时先 execute_sql 验证，确认无误再用 submit_sql 交卷。"""


def system_prompt(dialect: str) -> str:
    """方言版 baseline prompt + 工具说明。

    ``baseline_dialect.system_prompt("sqlite") == baseline.SYSTEM``，所以 SQLite 上
    的 prompt 和加工具之前的常量逐字节相同，已有数字不受影响。
    """
    return baseline_dialect.system_prompt(dialect) + TOOL_SUFFIX


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
    dialect: str,
    evidence: str = "",
    max_steps: int = MAX_STEPS_DEFAULT,
    max_tokens: int = 8192,
) -> Iterator[AgentEvent]:
    """跑一轮工具循环，产出事件流。最后必是一个 ``final`` 或 ``error``。

    ``dialect`` 决定 system prompt 里的方言名：PG 上跑却告诉模型"你是 SQLite 专家"
    不会报错，只会静默写错 SQL，所以这个参数不能由默认值兜住。
    """
    ev = f"\n业务口径说明：{evidence}\n" if evidence else ""
    messages = [Message.user(baseline.USER_TEMPLATE.format(
        schema=schema, evidence=ev, question=question))]
    usage = Usage()
    n_calls = 0

    for step in range(1, max_steps + 1):
        try:
            resp = provider.chat(
                system=system_prompt(dialect), messages=messages, tools=TOOLS,
                max_tokens=max_tokens,
            )
            # Usage.__add__ 遇到混合 cost_unit 会抛 ValueError。放在 try 里收敛成
            # error 事件，否则异常会冒出生成器、炸到评测主循环。
            usage = usage + resp.usage
        except LLMError as exc:
            yield AgentEvent("error", {"message": str(exc), "steps": step,
                                       "tool_calls": n_calls}, usage=usage)
            return
        except ValueError as exc:
            yield AgentEvent("error", {"message": f"用量聚合失败：{exc}",
                                       "steps": step, "tool_calls": n_calls},
                             usage=usage)
            return

        # 在模型返回**之后**发：这一步的产出（思考和文字）要跟着事件一起给出去，
        # 界面才显示得出"它这一步在想什么"。顺序没变——step_start 仍是每步第一个事件。
        yield AgentEvent("step_start", {
            "step": step, "reasoning": resp.reasoning, "text": resp.text or "",
        })

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
                # 未知工具也发一对事件：轨迹里要能看出模型喊了什么、被回了什么，
                # 否则消费方看到 tool_calls 计数涨了却少一段经过。
                yield AgentEvent("tool_call", {"name": tc.name, "args": tc.args})
                results.append(ToolResult(
                    call_id=tc.id, content=f"未知工具：{tc.name}", is_error=True))
                yield AgentEvent("tool_result", {
                    "name": tc.name, "ok": False, "rows": 0,
                    "error": f"未知工具：{tc.name}",
                })
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
