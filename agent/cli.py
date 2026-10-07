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
from rich.markup import escape
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


class Renderer:
    """逐事件渲染。**流式**——每来一个事件就打印，不攒到最后。

    攒到最后再打，会让人以为程序卡住了：一轮工具循环可能跑十几秒到几十秒。

    **模型说的话和查出来的数据都要 ``escape``**：它们里面可能有 ``[`` 之类的字符，
    rich 会当成标记去解析，轻则显示错乱，重则直接抛 MarkupError 把整个程序打断。
    """

    def __init__(self, *, show_thinking: bool = True) -> None:
        self.show_thinking = show_thinking
        self.step = 0

    def __call__(self, e: AgentEvent) -> None:
        p = e.payload
        if e.type == "step_start":
            self.step = p.get("step", self.step)
            console.print(f"\n[bold cyan]── 第 {self.step} 步 ──[/]")
            reasoning = (p.get("reasoning") or "").strip()
            if reasoning and self.show_thinking:
                console.print(Panel(escape(reasoning), title="[dim]思考[/]",
                                    title_align="left", border_style="dim", padding=(0, 1)))
            text = (p.get("text") or "").strip()
            if text:
                console.print(Panel(escape(text), title="[dim]输出[/]",
                                    title_align="left", border_style="dim", padding=(0, 1)))
        elif e.type == "tool_call":
            console.print(f"  [bold]▸ {escape(str(p.get('name', '')))}[/]")
            sql = (p.get("args") or {}).get("sql")
            if sql:
                console.print(Syntax(str(sql), "sql", theme="ansi_dark", word_wrap=True,
                                     padding=(0, 2)))
        elif e.type == "tool_result":
            if p.get("ok"):
                console.print(f"  [green]← {p.get('rows', 0)} 行[/]")
                preview = (p.get("preview") or "").strip()
                if preview:
                    console.print(Panel(escape(preview), border_style="green", padding=(0, 1)))
            else:
                console.print(f"  [red]← 失败：{escape(str(p.get('error', '')))}[/]")
        elif e.type == "error":
            console.print(Panel(f"[red]{escape(str(p.get('message', '')))}[/]",
                                title="[red]调用失败[/]", title_align="left",
                                border_style="red", padding=(0, 1)))


def render(events: Iterable[AgentEvent], *, show_thinking: bool = True) -> None:
    """渲染一整段事件（重放用）。实时跑走 ``Renderer`` 逐个喂，边跑边显示。"""
    r = Renderer(show_thinking=show_thinking)
    for e in events:
        r(e)


def _answer_panel(sql: str, sandbox: Sandbox | None) -> str:
    """渲染最终 SQL 和它的结果，**返回结果预览**——结论那一步要靠它给数字。"""
    if not sql:
        console.print(Panel("[yellow]没有产出 SQL[/]", border_style="yellow"))
        return ""
    console.print(Panel(Syntax(sql, "sql", theme="ansi_dark", word_wrap=True),
                        title="最终 SQL", title_align="left", border_style="cyan"))
    if sandbox is None:
        return ""
    res = sandbox.run(sql)
    if res.ok:
        md = res.to_markdown(max_rows=20)
        console.print(Panel(escape(md), title=f"答案 · {len(res.rows)} 行",
                            title_align="left", border_style="cyan", padding=(0, 1)))
        return md
    console.print(Panel(f"[red]{escape(res.error)}[/]", title="最终 SQL 跑不通",
                        title_align="left", border_style="red"))
    return ""


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


def _load_terms(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _terms_for(question: str, terms: dict[str, str]) -> str:
    """只把**问题里出现过**的术语拼进 prompt。

    整张术语表塞进去只会稀释注意力，而且术语多了 prompt 会失控。
    """
    hits = [f"- {t}：{d}" for t, d in terms.items() if t and t in question]
    return "已确认的业务口径：\n" + "\n".join(hits) if hits else ""


def _make_ask(terms: dict[str, str], path: Path | None):
    """做一个问用户的回调。答过的记进术语表，下次不再问。"""

    def _ask(args: dict) -> str:
        term = str(args.get("term", "")).strip()
        question = str(args.get("question", "")).strip()
        cands = [str(c).strip() for c in (args.get("candidates") or []) if str(c).strip()]
        body = question or f"「{term}」是什么意思？"
        for i, c in enumerate(cands, 1):
            body += f"\n  [bold]{i}.[/] {c}"
        body += f"\n  [bold]{len(cands) + 1}.[/] 其他（我来说）"
        console.print(Panel(body, title=f"❓ 需要确认：{term or '业务口径'}",
                            title_align="left", border_style="yellow", padding=(0, 1)))

        raw = console.input("[yellow]你的选择[/] > ").strip()
        answer = raw
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(cands):
                answer = cands[n - 1]
            elif n == len(cands) + 1:
                answer = console.input("  请说明：").strip()
        if not answer:
            answer = cands[0] if cands else "按最合理的解释"
        console.print(f"  [green]→ {answer}[/]")

        if term:
            terms[term] = answer
            if path is not None:
                path.write_text(json.dumps(terms, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        return f"用户确认：{term} = {answer}" if term else f"用户回答：{answer}"

    return _ask


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
    ap.add_argument("--terms", metavar="FILE", default="terms.json",
                    help="业务口径术语表（JSON）。模型不确定时会问用户，答过的记在这里，"
                         "下次提问自动带上、不再问。默认 terms.json")
    ap.add_argument("--no-ask", action="store_true",
                    help="不让模型问用户（工具集退回 execute_sql + submit_sql）")
    args = ap.parse_args(argv)

    deny = [c for c in args.deny_columns.split(",") if c.strip()]

    if args.replay:
        raw = json.loads(Path(args.replay).read_text(encoding="utf-8"))
        console.print(Panel(f"[bold]{raw.get('question', '')}[/]\n[dim]{raw.get('db', '')}[/]",
                            title="重放", title_align="left", border_style="blue", padding=(0, 1)))
        render(_events_from_trace(raw.get("events") or []), show_thinking=not args.no_thinking)
        preview = raw.get("preview") or ""
        _answer_panel(raw.get("final_sql", ""), None)
        if preview:
            console.print(Panel(escape(preview), title="答案", title_align="left",
                                border_style="cyan", padding=(0, 1)))
        if raw.get("conclusion"):
            console.print(Panel(escape(raw["conclusion"]), title="结论", title_align="left",
                                border_style="green", padding=(0, 1)))
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

    terms_path = Path(args.terms) if args.terms else None
    terms = _load_terms(terms_path)
    evidence = _terms_for(args.question, terms)
    if evidence:
        console.print(f"[dim]带上已确认的业务口径 {evidence.count('- ')} 条[/]")
    ask = None if args.no_ask else _make_ask(terms, terms_path)

    renderer = Renderer(show_thinking=not args.no_thinking)
    events: list[AgentEvent] = []
    started = time.perf_counter()
    for e in core.run(
        args.question, provider=provider, sandbox=sandbox, schema=schema,
        dialect=sandbox.dialect, evidence=evidence, max_steps=args.max_steps,
        max_tokens=router.max_tokens_for("sql_gen"), ask=ask,
    ):
        events.append(e)
        renderer(e)          # 边跑边打，不攒到最后
    wall = time.perf_counter() - started

    out = core.consume(iter(events))
    preview = _answer_panel(out.sql, sandbox)
    # 结论在循环之外：循环只负责产出 SQL，结论要等 SQL 真跑完才有数据可依据。
    conclusion, _ = core.conclude(args.question, out.sql, preview, provider=provider)
    if conclusion:
        console.print(Panel(escape(conclusion), title="结论", title_align="left",
                            border_style="green", padding=(0, 1)))
    _cost_panel(out, wall)

    if args.save_trace:
        Path(args.save_trace).write_text(json.dumps({
            "question": args.question, "db": str(args.db), "dialect": sandbox.dialect,
            "events": _trace_events(events), "final_sql": out.sql,
            "preview": preview, "conclusion": conclusion,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        console.print(f"[dim]轨迹已存到 {args.save_trace}（可用 --replay 重放，不花钱）[/]")

    return 0 if out.sql else 1


if __name__ == "__main__":
    sys.exit(main())
