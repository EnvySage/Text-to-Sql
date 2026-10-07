"""禁查列：在沙箱前面再加一道业务口径的门。

``guard`` 管"能不能写"（只放行单条 SELECT），``Sandbox`` 的第二道防线管"连接权限"。
这里管的是第三件事：**能不能看**。

接含隐私数据的库（用户日记、API 密钥、向量列）时用得上——那些列查出来要么泄露，
要么直接爆掉上下文（``vector(1024)``）。
"""

from __future__ import annotations

import re
from typing import Iterable

from sandbox.base import ExecResult, Sandbox


class DenyColumns:
    """按列名拦查询——把沙箱包一层。

    **词边界匹配，不解析 SQL**：粗，但对"别把日记原文查出来"这个目的够用，
    而且不会因为解析器理解偏差而漏。宁可误拦，不可放过。

    **必须按词边界，不能按子串**：实测过——子串匹配会把 ``metadata`` 当成 ``data``、
    把 ``content_hit_count`` 当成 ``content`` 拦下来。列名里的 ``_`` 算词字符，
    所以 ``\\b`` 正好卡在列名边界上，不会误伤。
    """

    def __init__(self, sandbox: Sandbox, columns: Iterable[str]) -> None:
        self._sb = sandbox
        self._cols = sorted({c.strip().lower() for c in columns if c.strip()})
        self._pattern = (
            re.compile(r"\b(" + "|".join(re.escape(c) for c in self._cols) + r")\b")
            if self._cols else None
        )

    @property
    def dialect(self) -> str:
        return self._sb.dialect

    @property
    def denied(self) -> list[str]:
        return list(self._cols)

    def run(self, sql: str, *, enforce_limit: bool = True) -> ExecResult:
        hit = self._pattern.search(sql.lower()) if self._pattern else None
        if hit:
            return ExecResult(
                ok=False, sql=sql,
                error=f"列 {hit.group(1)!r} 被禁查（可能含隐私数据）。换一列，或让用户解除限制。",
            )
        return self._sb.run(sql, enforce_limit=enforce_limit)
