"""命令行界面：把 agent 干活的过程渲染出来。

只渲染，不含逻辑——事件流由 ``agent/core.py`` 产出，这里只负责显示。
一套事件流可以同时被人看、被存、被回放（见 DESIGN 4.4）。

    uv run python -m agent.cli --db <库路径或连接地址> --question "..."
    uv run python -m agent.cli --replay trace.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, Iterator

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from agent import core
from agent.events import AgentEvent
from agent.schema import schema_text
from llm.router import Router
from sandbox import open_sandbox
from sandbox.base import Sandbox
from sandbox.deny import DenyColumns

console = Console()


def render(events: Iterable[AgentEvent], *, show_thinking: bool = True) -> None:
    """渲染事件流。实时跑和重放走的是同一个函数。"""
    step = 0
    for e in events:
        p = e.payload
        if e.type == "step_start":
            step = p.get("step", step)
            console.print(f"\n[bold cyan]── 第 {step} 步 ──[/]")
            reasoning = (p.get("reasoning") or "").strip()
            if reasoning and show_thinking:
                console.print(Panel(reasoning, title="[dim]思考[/]", title_align="left",
                                    border_style="dim", padding=(0, 1)))
            text = (p.get("text") or "").strip()
            if text:
                console.print(Panel(text, title="[dim]输出[/]", title_align="left",
                                    border_style="dim", padding=(0, 1)))
        elif e.type == "tool_call":
            console.print(f"  [bold]▸ {p.get('name', '')}[/]")
            sql = (p.get("args") or {}).get("sql")
            if sql:
                console.print(Syntax(str(sql), "sql", theme="ansi_dark", word_wrap=True,
                                     padding=(0, 2)))
        elif e.type == "tool_result":
            if p.get("ok"):
                console.print(f"  [green]← {p.get('rows', 0)} 行[/]")
                preview = (p.get("preview") or "").strip()
                if preview:
                    console.print(Panel(preview, border_style="green", padding=(0, 1)))
            else:
                console.print(f"  [red]← 失败：{p.get('error', '')}[/]")
        elif e.type == "error":
            console.print(Panel(f"[red]{p.get('message', '')}[/]", title="[red]调用失败[/]",
                                title_align="left", border_style="red", padding=(0, 1)))


def _answer_panel(sql: str, sandbox: Sandbox | None) -> None:
    """把最终 SQL 真跑一遍，把答案显示出来。没有沙箱（重放）时只显示 SQL。"""
    if not sql:
        console.print(Panel("[yellow]没有产出 SQL[/]", border_style="yellow"))
        return
    console.print(Panel(Syntax(sql, "sql", theme="ansi_dark", word_wrap=True),
                        title="最终 SQL", title_align="left", border_style="cyan"))
    if sandbox is None:
        return
    res = sandbox.run(sql)
    if res.ok:
        console.print(Panel(res.to_markdown(max_rows=20), title=f"答案 · {len(res.rows)} 行",
                            title_align="left", border_style="cyan", padding=(0, 1)))
    else:
        console.print(Panel(f"[red]{res.error}[/]", title="最终 SQL 跑不通",
                            title_align="left", border_style="red"))


def _cost_panel(out: core.AgentOutcome, wall_s: float) -> None:
    u = out.usage
    console.print(Panel(
        f"步数 {out.steps} · 工具调用 {out.tool_calls}"
        + ("  [red]（撞到上限，没收敛）[/]" if out.hit_cap else "")
        + f" · 耗时 {wall_s:.1f}s\n"
        f"token  输入 {u.input_tokens:,} · 输出 {u.output_tokens:,} · 思考 {u.reasoning_tokens:,}",
        title="这一问的账", title_align="left", border_style="dim", padding=(0, 1),
    ))


def _trace_events(events: list[AgentEvent]) -> list[dict]:
    return [{"type": e.type, "payload": e.payload} for e in events]


def _events_from_trace(raw: list[dict]) -> Iterator[AgentEvent]:
    for r in raw:
        yield AgentEvent(r["type"], r.get("payload") or {})


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="数据分析 agent 的命令行界面")
    ap.add_argument("--db", help="库路径（SQLite 文件）或连接地址（postgresql://...）")
    ap.add_argument("--question", help="要问的问题")
    ap.add_argument("--max-steps", type=int, default=core.MAX_STEPS_DEFAULT,
                    help="循环上限，防跑飞；不是预算")
    ap.add_argument("--save-trace", metavar="FILE", help="把这次的过程存成 JSON，之后可 --replay")
    ap.add_argument("--replay", metavar="FILE", help="播放之前存下的轨迹（不连模型、不花钱）")
    ap.add_argument("--no-thinking", action="store_true", help="不显示思考内容")
    ap.add_argument("--deny-columns", default="",
                    help="禁查列名（逗号分隔）。会碰到这些列的查询直接被拒——"
                         "给含隐私数据的库用，如 content,embedding,password_hash")
    args = ap.parse_args(argv)

    deny = [c for c in args.deny_columns.split(",") if c.strip()]

    if args.replay:
        raw = json.loads(Path(args.replay).read_text(encoding="utf-8"))
        console.print(Panel(f"[bold]{raw.get('question', '')}[/]\n[dim]{raw.get('db', '')}[/]",
                            title="重放", title_align="left", border_style="blue", padding=(0, 1)))
        render(_events_from_trace(raw.get("events") or []), show_thinking=not args.no_thinking)
        _answer_panel(raw.get("final_sql", ""), None)
        return 0

    if not args.db or not args.question:
        console.print("[red]--db 和 --question 都要给（或用 --replay）[/]")
        return 2

    sandbox: Sandbox = open_sandbox(args.db)
    if deny:
        sandbox = DenyColumns(sandbox, deny)
    schema = schema_text(args.db)
    router = Router.from_file()
    provider = router.for_role("sql_gen")

    console.print(Panel(
        f"[bold]{args.question}[/]\n"
        f"[dim]{args.db} · {sandbox.dialect} · {provider.model} · schema {len(schema):,} 字符[/]"
        + (f"\n[dim]禁查列：{', '.join(deny)}[/]" if deny else ""),
        title="数据分析 agent", title_align="left", border_style="blue", padding=(0, 1),
    ))

    import time

    started = time.perf_counter()
    events = list(core.run(
        args.question, provider=provider, sandbox=sandbox, schema=schema,
        dialect=sandbox.dialect, max_steps=args.max_steps,
        max_tokens=router.max_tokens_for("sql_gen"),
    ))
    wall = time.perf_counter() - started

    render(events, show_thinking=not args.no_thinking)
    out = core.consume(iter(events))
    _answer_panel(out.sql, sandbox)
    _cost_panel(out, wall)

    if args.save_trace:
        Path(args.save_trace).write_text(json.dumps({
            "question": args.question, "db": str(args.db), "dialect": sandbox.dialect,
            "events": _trace_events(events), "final_sql": out.sql,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        console.print(f"[dim]轨迹已存到 {args.save_trace}（可用 --replay 重放，不花钱）[/]")

    return 0 if out.sql else 1


if __name__ == "__main__":
    sys.exit(main())
