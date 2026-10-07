"""禁查列：在沙箱前面再加一道业务口径的门。

``guard`` 管"能不能写"（只放行单条 SELECT），``Sandbox`` 的第二道防线管"连接权限"。
这里管的是第三件事：**能不能看**。

接含隐私数据的库（用户日记、API 密钥、向量列）时用得上——那些列查出来要么泄露，
要么直接爆掉上下文（``vector(1024)``）。
"""

from __future__ import annotations

from typing import Iterable

from sandbox.base import ExecResult, Sandbox


class DenyColumns:
    """按列名拦查询——把沙箱包一层。

    **文本匹配，不解析 SQL**：粗，但对"别把日记原文查出来"这个目的够用，
    而且不会因为解析器理解偏差而漏。宁可误拦，不可放过。

    代价：列名是子串匹配，``content`` 会连 ``content_hit_count`` 一起拦掉。
    禁查名单要按库的实际列名挑，别放太泛的词。
    """

    def __init__(self, sandbox: Sandbox, columns: Iterable[str]) -> None:
        self._sb = sandbox
        self._cols = sorted({c.strip().lower() for c in columns if c.strip()})

    @property
    def dialect(self) -> str:
        return self._sb.dialect

    @property
    def denied(self) -> list[str]:
        return list(self._cols)

    def run(self, sql: str, *, enforce_limit: bool = True) -> ExecResult:
        low = sql.lower()
        hit = next((c for c in self._cols if c in low), None)
        if hit:
            return ExecResult(
                ok=False, sql=sql,
                error=f"列 {hit!r} 被禁查（可能含隐私数据）。换一列，或让用户解除限制。",
            )
        return self._sb.run(sql, enforce_limit=enforce_limit)
