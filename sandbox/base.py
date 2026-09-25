"""各数据库执行器共用的契约。

上层（agent、eval）只依赖 ``Sandbox`` 协议，不关心背后是 SQLite 还是服务端库。
每种库的连接、只读、超时手段各不相同，但对外必须是同一套行为：
单条只读查询、失败返回结果对象、行数有上限且能判断是否被截断。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sandbox.guard import check, ensure_limit

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_ROWS = 2000


@dataclass(slots=True)
class ExecResult:
    """一次执行的结果。

    失败不抛异常：``error`` 里的文字要原样回灌给模型，让它据此改写 SQL。
    这正是"执行反馈重试"能把准确率拉起来的原因。
    """

    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    error: str = ""
    truncated: bool = False
    elapsed_ms: float = 0.0
    sql: str = ""

    def to_markdown(self, max_rows: int = 20) -> str:
        """给模型看的紧凑表示。宽表全量塞进 context 太贵。"""
        if not self.ok:
            return f"ERROR: {self.error}"
        if not self.rows:
            return "（查询成功，但没有返回任何行）"

        head = self.rows[:max_rows]
        lines = [" | ".join(self.columns)]
        lines += [" | ".join("NULL" if v is None else str(v) for v in r) for r in head]
        if len(self.rows) > max_rows:
            lines.append(f"... 另有 {len(self.rows) - max_rows} 行未显示")
        if self.truncated:
            lines.append(f"（结果已被截断至 {len(self.rows)} 行上限）")
        return "\n".join(lines)


@runtime_checkable
class Sandbox(Protocol):
    """只读 SQL 执行器。

    ``dialect`` 用 sqlglot 的方言名（"sqlite" / "postgres" / "mysql"），
    必须一路传给 guard：guard 会重新生成 SQL，按错的方言生成会静默改变语义。
    """

    dialect: str

    def run(self, sql: str, *, enforce_limit: bool = True) -> ExecResult: ...


class QueryFailed(Exception):
    """实现类把驱动层的失败翻译成给模型看的一句话，由 ``BaseSandbox.run()`` 收敛成 ``ExecResult``。"""


class BaseSandbox:
    """各实现共用的执行流程：guard → 补 LIMIT → 多取一行判截断。

    截断检测错了会静默产出假数字，所以只在这里写一份。
    子类只负责怎么连库、怎么限时、怎么把驱动异常翻译成人话，都在 ``_fetch`` 里。
    """

    dialect: str = ""

    def __init__(
        self, *, timeout: float = DEFAULT_TIMEOUT, max_rows: int = DEFAULT_MAX_ROWS
    ) -> None:
        self.timeout = timeout
        self.max_rows = max_rows

    def run(self, sql: str, *, enforce_limit: bool = True) -> ExecResult:
        verdict = check(sql, dialect=self.dialect)
        if not verdict.ok:
            return ExecResult(ok=False, error=f"被安全策略拒绝：{verdict.reason}", sql=sql)

        # 注入的上限要比 max_rows 多 1：多取到的那一行是"结果被截断了"的证据。
        # 直接注入 max_rows 的话，恰好取满时无法区分"刚好这么多"和"被截断"。
        final_sql = (
            ensure_limit(sql, self.max_rows + 1, dialect=self.dialect)
            if enforce_limit else sql
        )

        started = time.perf_counter()
        try:
            columns, rows = self._fetch(final_sql, self.max_rows + 1)
        except QueryFailed as exc:
            return ExecResult(
                ok=False, error=str(exc),
                elapsed_ms=(time.perf_counter() - started) * 1000, sql=final_sql,
            )
        return ExecResult(
            ok=True,
            columns=columns,
            rows=[tuple(r) for r in rows[: self.max_rows]],
            truncated=len(rows) > self.max_rows,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            sql=final_sql,
        )

    def _fetch(self, sql: str, n: int) -> tuple[list[str], list[tuple[Any, ...]]]:
        """执行 ``sql``，最多取 ``n`` 行。失败时抛 ``QueryFailed``，其余异常说明是代码 bug，照常抛出。"""
        raise NotImplementedError
