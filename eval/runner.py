"""批量评测。

产出三个指标，缺一个都不够：

- **执行准确率**：预测 SQL 的结果集和标准 SQL 一致的比例。这是主指标。
- **有效执行率**：SQL 能跑通不报错的比例。它和准确率的差值告诉你问题出在哪——
  差值大说明模型语法没问题但理解错了题意，差值小说明连 SQL 都写不对。
- **单题成本**：含重试的平均花费。没有它，一个"准确率提升 5 点"的改动
  可能其实是成本翻了三倍换来的。

每题的完整记录都落盘成 JSONL，之后可以不重跑就做复盘和对比。

    uv run python -m eval.runner --dataset <BIRD目录> --limit 50
"""

from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from rich.console import Console
from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn,
)
from rich.table import Table

from agent.baseline_dialect import generate_sql
from agent.schema import schema_text
from eval.dataset import Item, load_bird
from eval.metrics import METRIC_VERSION, needs_order, result_match
from llm.base import LLMError, Usage
from llm.router import Router
from sandbox import open_sandbox

console = Console()

CALL_FAILED = "模型调用失败"


@dataclass
class Record:
    qid: str
    db_id: str
    question: str
    difficulty: str
    gold_sql: str
    pred_sql: str
    correct: bool
    executable: bool
    reason: str
    elapsed_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    cost_unit: str = "unknown"
    schema_chars: int = 0
    error: str = ""


@dataclass
class Summary:
    total: int = 0
    correct: int = 0
    executable: int = 0
    gold_failed: int = 0
    usage: Usage = field(default_factory=Usage)
    wall_s: float = 0.0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def exec_rate(self) -> float:
        return self.executable / self.total if self.total else 0.0


def run_one(item: Item, router: Router, *, sample_rows: int, max_rows: int) -> Record:
    """跑一道题：生成 SQL -> 执行 -> 和标准答案比对。

    任何异常都收敛成一条失败记录。一道题炸掉不能让整轮评测中断——
    跑了一半的评测既浪费钱又得不到结论。
    """
    started = time.perf_counter()
    sandbox = open_sandbox(item.db, max_rows=max_rows)  # type: ignore[arg-type]

    try:
        schema = schema_text(item.db, sample_rows=sample_rows)  # type: ignore[arg-type]
    except Exception as exc:
        return Record(
            qid=item.qid, db_id=item.db_id, question=item.question,
            difficulty=item.difficulty, gold_sql=item.gold_sql, pred_sql="",
            correct=False, executable=False, reason="读取 schema 失败",
            elapsed_ms=(time.perf_counter() - started) * 1000, error=str(exc),
        )

    try:
        gen = generate_sql(
            router.for_role("sql_gen"),
            dialect=sandbox.dialect,
            schema=schema, question=item.question, evidence=item.evidence,
            max_tokens=router.max_tokens_for("sql_gen"),
        )
    except LLMError as exc:
        return Record(
            qid=item.qid, db_id=item.db_id, question=item.question,
            difficulty=item.difficulty, gold_sql=item.gold_sql, pred_sql="",
            correct=False, executable=False, reason=CALL_FAILED,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            schema_chars=len(schema), error=str(exc),
        )

    u = gen.usage
    rec = Record(
        qid=item.qid, db_id=item.db_id, question=item.question,
        difficulty=item.difficulty, gold_sql=item.gold_sql, pred_sql=gen.sql,
        correct=False, executable=False, reason="",
        elapsed_ms=(time.perf_counter() - started) * 1000,
        input_tokens=u.input_tokens, output_tokens=u.output_tokens,
        cached_tokens=u.cached_input_tokens, reasoning_tokens=u.reasoning_tokens,
        cost=u.cost, cost_unit=u.cost_unit, schema_chars=len(schema),
        error=gen.error,
    )

    if not gen.sql:
        rec.reason = "没有生成出 SQL"
        return rec

    pred = sandbox.run(gen.sql)
    rec.executable = pred.ok
    if not pred.ok:
        rec.reason = f"预测 SQL 执行失败：{pred.error[:160]}"
        return rec

    # 标准答案也要真跑一遍。标准 SQL 自己跑不通的题必须剔除，
    # 否则分母里混着无解题，准确率永远上不去而且找不到原因。
    gold = sandbox.run(item.gold_sql)
    if not gold.ok:
        rec.reason = f"标准 SQL 执行失败（该题作废）：{gold.error[:160]}"
        return rec

    m = result_match(
        pred.rows, gold.rows, order_sensitive=needs_order(item.question)
    )
    rec.correct = m.match
    rec.reason = m.reason or ("一致" if m.match else "不一致")
    rec.elapsed_ms = (time.perf_counter() - started) * 1000
    return rec


def run_all(
    items: list[Item],
    run: Callable[[Item], Record],
    *,
    workers: int,
    stop_after: int = 0,
    on_done: Callable[[list[Record]], None] | None = None,
) -> tuple[list[Record], str]:
    """并发跑完所有题；连续 ``stop_after`` 题模型调用失败就熔断（0 表示不熔断）。

    熔断时不再派发新题，已经在跑的题照常做完，已完成的记录照常返回。
    限流时如果硬杀进程，前面已经花钱跑完的题也会跟着丢掉。
    返回（记录，提前停止的原因；正常跑完为空串）。
    """
    records: list[Record] = []
    aborted = ""
    streak = 0
    pending = iter(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # 同时只派发 workers 道题，做完一道再派一道。一次性全部排进队列的话，
        # 熔断时得去撤销排队的任务，而撤销和任务开始执行之间有竞态。
        running = {pool.submit(run, it) for it in itertools.islice(pending, workers)}
        while running:
            done, running = wait(running, return_when=FIRST_COMPLETED)
            for fut in done:
                rec = fut.result()
                records.append(rec)
                streak = streak + 1 if rec.reason == CALL_FAILED else 0
                if on_done:
                    on_done(records)
                if stop_after and streak >= stop_after and not aborted:
                    aborted = f"连续 {streak} 题{CALL_FAILED}（疑似限流），提前停止"
            if not aborted:
                running |= {pool.submit(run, it) for it in itertools.islice(pending, len(done))}
    return records, aborted


def git_version() -> str:
    """当前代码版本：短 commit 号，工作区有未提交改动时加 ``+dirty``。

    结果文件靠它追溯"这个数字是哪版代码跑出来的"。``eval/results/`` 不算改动：
    上一次跑出来还没提交的结果文件，不代表代码变了。
    """
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", ".", ":!eval/results"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{commit}+dirty" if dirty else commit


def summarize(records: list[Record], wall_s: float) -> Summary:
    s = Summary(wall_s=wall_s)
    for r in records:
        if "该题作废" in r.reason:
            s.gold_failed += 1
            continue
        s.total += 1
        s.correct += int(r.correct)
        s.executable += int(r.executable)
        s.usage = s.usage + Usage(
            input_tokens=r.input_tokens, output_tokens=r.output_tokens,
            cached_input_tokens=r.cached_tokens, reasoning_tokens=r.reasoning_tokens,
            cost=r.cost, cost_unit=r.cost_unit,  # type: ignore[arg-type]
        )
    return s


def print_report(records: list[Record], s: Summary, label: str) -> None:
    t = Table(title=f"评测结果 · {label}", header_style="bold")
    t.add_column("指标"); t.add_column("数值", justify="right")
    t.add_row("有效题数", str(s.total))
    t.add_row("[bold]执行准确率[/]", f"[bold]{s.accuracy:.1%}[/] ({s.correct}/{s.total})")
    t.add_row("有效执行率", f"{s.exec_rate:.1%} ({s.executable}/{s.total})")
    if s.gold_failed:
        t.add_row("标准SQL跑不通(已剔除)", str(s.gold_failed))
    t.add_row("总成本", f"{s.usage.cost:.4f} {s.usage.cost_unit}")
    if s.total:
        t.add_row("单题成本", f"{s.usage.cost / s.total:.5f} {s.usage.cost_unit}")
        t.add_row("单题 token", f"入 {s.usage.input_tokens // s.total} / 出 {s.usage.output_tokens // s.total}")
        t.add_row("其中缓存命中", f"{s.usage.cached_input_tokens / max(s.usage.input_tokens, 1):.1%}")
        t.add_row("其中思考 token", str(s.usage.reasoning_tokens // s.total))
        t.add_row("平均耗时", f"{sum(r.elapsed_ms for r in records) / len(records) / 1000:.1f}s")
    t.add_row("总墙钟", f"{s.wall_s:.0f}s")
    console.print(t)

    # 失败归因：准确率掉在哪一类，决定下一步优先修什么
    buckets: dict[str, int] = {}
    for r in records:
        if r.correct or "该题作废" in r.reason:
            continue
        if "没有生成出 SQL" in r.reason:
            k = "没吐出 SQL"
        elif "执行失败" in r.reason:
            k = "SQL 跑不通"
        elif "行数不同" in r.reason:
            k = "结果行数不对"
        elif "列数不同" in r.reason:
            k = "结果列数不对"
        elif "模型调用失败" in r.reason:
            k = "模型调用失败"
        else:
            k = "结果内容不对"
        buckets[k] = buckets.get(k, 0) + 1

    if buckets:
        f = Table(title="失败归因", header_style="bold")
        f.add_column("类型"); f.add_column("题数", justify="right"); f.add_column("占错误比", justify="right")
        wrong = sum(buckets.values())
        for k, v in sorted(buckets.items(), key=lambda x: -x[1]):
            f.add_row(k, str(v), f"{v / wrong:.0%}")
        console.print(f)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="批量评测 text-to-SQL")
    ap.add_argument("--dataset", required=True, help="BIRD 数据集根目录")
    ap.add_argument("--questions", default=None,
                    help="题目文件名（相对 --dataset），如 mini_dev_postgresql.json；不给就自动找")
    ap.add_argument("--pg-dsn", default=None,
                    help="在 PostgreSQL 上跑：只读账号的连接地址，每道题自动加 search_path=<db_id>")
    ap.add_argument("--limit", type=int, default=50, help="抽样题数（固定种子）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=2,
                    help="并发数。实测网关并发 4 会返回 503 no_healthy_account")
    ap.add_argument("--sample-rows", type=int, default=0, help="schema 里附带几行样例数据")
    ap.add_argument("--max-rows", type=int, default=2000)
    ap.add_argument("--stop-after-call-failures", type=int, default=3,
                    help="连续这么多题模型调用失败（通常是限流）就停止派发新题，已完成的照常保存；0 表示不熔断")
    ap.add_argument("--label", default="baseline", help="本次实验的名字，写进结果文件名")
    ap.add_argument("--out", default="eval/results")
    args = ap.parse_args(argv)

    code_version = git_version()
    items = load_bird(
        args.dataset, limit=args.limit, seed=args.seed,
        questions_file=args.questions, pg_dsn=args.pg_dsn,
    )
    dialect = "postgres" if args.pg_dsn else "sqlite"
    router = Router.from_file()
    provider = router.for_role("sql_gen")
    console.print(
        f"[bold]{args.label}[/] · {len(items)} 题 · "
        f"{len({i.db_id for i in items})} 个库 · {dialect} · 模型 {provider.model} · 并发 {args.workers}"
    )

    started = time.perf_counter()
    with Progress(
        TextColumn("[progress.description]{task.description}"), BarColumn(),
        MofNCompleteColumn(), TimeElapsedColumn(), console=console,
    ) as bar:
        task = bar.add_task("评测中", total=len(items))

        def on_done(recs: list[Record]) -> None:
            ok = sum(r.correct for r in recs)
            bar.update(task, advance=1, description=f"评测中 准确率 {ok / len(recs):.0%}")

        records, aborted = run_all(
            items,
            lambda it: run_one(
                it, router, sample_rows=args.sample_rows, max_rows=args.max_rows
            ),
            workers=args.workers,
            stop_after=args.stop_after_call_failures,
            on_done=on_done,
        )
    wall = time.perf_counter() - started

    records.sort(key=lambda r: (r.db_id, r.qid))
    s = summarize(records, wall)

    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{args.label}-{stamp}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "label": args.label, "model": provider.model, "n": len(items),
            "limit": args.limit, "seed": args.seed, "sample_rows": args.sample_rows,
            "dialect": dialect, "questions": args.questions,
            "accuracy": s.accuracy, "exec_rate": s.exec_rate,
            "cost": s.usage.cost, "cost_unit": s.usage.cost_unit,
            "gold_failed": s.gold_failed, "wall_s": wall,
            "completed": len(records), "aborted": aborted,
            "git_commit": code_version, "metric_version": METRIC_VERSION,
        }, ensure_ascii=False) + "\n")
        for r in records:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

    print_report(records, s, args.label)
    if aborted:
        console.print(
            f"[bold red]{aborted}：只完成 {len(records)}/{len(items)} 题，本次数字不得引用。[/]"
        )
    console.print(f"\n明细已写入 [bold]{path}[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
