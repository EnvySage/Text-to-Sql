"""provider 层的端到端往返校验。

对每一个配好 key 的 provider 跑同一段两轮工具调用，并排打出 token 和成本。
在相信任何评测数字之前先跑这个——适配器坏掉时产出的是看起来合理的垃圾。

    uv run python smoke.py
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from llm.base import LLMError, Message, ToolResult, ToolSpec

load_dotenv()
console = Console()

WEATHER = ToolSpec(
    name="get_row_count",
    description="返回销售数据库中某张表的行数。",
    parameters={
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "表名，例如 'orders'。"}
        },
        "required": ["table"],
    },
)

SYSTEM = (
    "You are a data analyst. Use the provided tools to answer. "
    "When you have the number, state it in one short sentence."
)
QUESTION = "How many rows are in the orders table?"


def candidates():
    out = []
    if os.environ.get("LOCAL_API_KEY"):
        from llm.openai_compat import OpenAICompatProvider

        for model in (os.environ.get("SMOKE_MODELS") or os.environ["LOCAL_MODEL"]).split(","):
            out.append(
                OpenAICompatProvider(
                    model=model.strip(),
                    base_url="local",
                    api_key_env="LOCAL_API_KEY",
                    name="local",
                    timeout=300.0,
                )
            )
    if os.environ.get("DEEPSEEK_API_KEY"):
        from llm.openai_compat import OpenAICompatProvider

        out.append(
            OpenAICompatProvider(
                model="deepseek-chat",
                base_url="deepseek",
                api_key_env="DEEPSEEK_API_KEY",
                name="deepseek",
            )
        )
    if os.environ.get("ANTHROPIC_API_KEY"):
        from llm.anthropic import AnthropicProvider

        out.append(AnthropicProvider(model="claude-sonnet-5"))
    if os.environ.get("OPENAI_API_KEY"):
        from llm.openai_compat import OpenAICompatProvider

        out.append(
            OpenAICompatProvider(
                model="gpt-4o-mini", base_url="openai", name="openai"
            )
        )
    return out


def exercise(p) -> dict:
    messages = [Message.user(QUESTION)]

    first = p.chat(system=SYSTEM, messages=messages, tools=[WEATHER], max_tokens=1024)
    if not first.tool_calls:
        return {
            "ok": False,
            "why": f"no tool call (stop={first.stop_reason}, text={first.text!r:.60})",
            "usage": first.usage,
        }

    call = first.tool_calls[0]
    messages.append(first.to_message())
    messages.append(
        Message.results([ToolResult(call_id=call.id, content='{"rows": 41823}')])
    )

    second = p.chat(system=SYSTEM, messages=messages, tools=[WEATHER], max_tokens=1024)
    total = first.usage + second.usage
    return {
        "ok": "41823" in (second.text or "") or "41,823" in (second.text or ""),
        "why": (second.text or "").strip().replace("\n", " ")[:70],
        "tool": f"{call.name}({call.args})",
        "usage": total,
        "first_usage": first.usage,
        "second_usage": second.usage,
    }


def main() -> int:
    providers = candidates()
    if not providers:
        console.print(
            "[yellow]没有找到任何 API key。[/] 把 .env.example 复制成 .env，"
            "至少填上 LOCAL_API_KEY。"
        )
        return 2

    table = Table(title="provider round-trip", header_style="bold")
    for col in ("provider", "model", "tool call", "in", "cached", "out", "think", "cost", "result"):
        table.add_column(col, overflow="fold")

    failures = 0
    for p in providers:
        try:
            r = exercise(p)
        except LLMError as exc:
            failures += 1
            table.add_row(p.name, p.model, "-", "-", "-", "-", "-", "-", f"[red]{exc}[/]")
            continue

        u = r["usage"]
        mark = "[green]PASS[/]" if r["ok"] else "[red]FAIL[/]"
        failures += 0 if r["ok"] else 1
        table.add_row(
            p.name,
            p.model,
            r.get("tool", "-"),
            str(u.input_tokens),
            str(u.cached_input_tokens),
            str(u.output_tokens),
            str(u.reasoning_tokens),
            f"{u.cost:.4f} {u.cost_unit}",
            f"{mark} {r['why']}",
        )

    console.print(table)
    console.print(
        "\n[dim]cached>0 on a second run means prompt caching is working "
        "(DeepSeek caches automatically).[/]"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
