"""runner 的熔断：限流时停止派发新题，但已经花钱跑完的题一条都不能丢。"""

from __future__ import annotations

from eval.dataset import Item
from eval.runner import CALL_FAILED, Record, run_all


def _items(n: int) -> list[Item]:
    return [Item(qid=str(i), db_id="db", question="q", gold_sql="SELECT 1") for i in range(n)]


def _record(it: Item, reason: str) -> Record:
    return Record(
        qid=it.qid, db_id=it.db_id, question=it.question, difficulty="",
        gold_sql=it.gold_sql, pred_sql="", correct=reason == "一致",
        executable=True, reason=reason, elapsed_ms=0.0,
    )


def test_all_ok_runs_everything():
    records, aborted = run_all(_items(20), lambda it: _record(it, "一致"), workers=2, stop_after=3)
    assert len(records) == 20 and aborted == ""


def test_consecutive_call_failures_stop_the_run_and_keep_finished_records():
    """从第 10 题开始全部限流：停在失败 3 题左右，前 10 题的结果必须保留。"""
    def run(it: Item) -> Record:
        return _record(it, CALL_FAILED if int(it.qid) >= 10 else "一致")

    records, aborted = run_all(_items(50), run, workers=1, stop_after=3)
    assert aborted and "提前停止" in aborted
    # 单并发下行为确定：10 题成功 + 3 题失败，之后不再派发
    assert len(records) == 13
    assert sum(r.correct for r in records) == 10


def test_scattered_failures_do_not_stop_the_run():
    def run(it: Item) -> Record:
        return _record(it, CALL_FAILED if int(it.qid) % 2 else "一致")

    records, aborted = run_all(_items(30), run, workers=1, stop_after=3)
    assert len(records) == 30 and aborted == ""


def test_zero_disables_the_breaker():
    records, aborted = run_all(
        _items(10), lambda it: _record(it, CALL_FAILED), workers=1, stop_after=0
    )
    assert len(records) == 10 and aborted == ""


def test_breaker_with_two_workers_stops_dispatching():
    def run(it: Item) -> Record:
        return _record(it, CALL_FAILED if int(it.qid) >= 10 else "一致")

    records, aborted = run_all(_items(50), run, workers=2, stop_after=3)
    assert aborted
    # 并发时同一批完成的题处理顺序不固定，"连续"按完成顺序算，熔断点会前后差一两题；
    # 要保证的是：确实提前停了，而且已经跑完的成功题一条不丢。
    assert len(records) < 20
    assert sum(r.correct for r in records) == 10


def test_git_version_names_a_commit():
    """结果文件靠它追溯代码版本：在仓库里跑必须拿到 commit 号，不能是 unknown。"""
    import re

    from eval.runner import git_version

    assert re.fullmatch(r"[0-9a-f]{7,}(\+dirty)?", git_version())
