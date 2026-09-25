"""只读 SQL 执行器（SQLite）。

三道护栏，缺一不可：
1. 只读连接（SQLite 的 ``mode=ro`` URI），写操作在驱动层就会被拒；
2. 超时中断，防止模型写出笛卡尔积把进程挂死；
3. 行数上限，防止百万行结果撑爆 context。

guard 的 AST 白名单是第一道，这里是第二道。两道都要有：
AST 挡的是语义，连接权限挡的是万一 AST 有洞。
公共流程（guard、补 LIMIT、截断检测）在 ``sandbox/base.py``。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from sandbox.base import (
    DEFAULT_MAX_ROWS, DEFAULT_TIMEOUT, BaseSandbox, ExecResult, QueryFailed,
)

__all__ = ["DEFAULT_MAX_ROWS", "DEFAULT_TIMEOUT", "ExecResult", "SQLiteSandbox"]


class SQLiteSandbox(BaseSandbox):
    dialect = "sqlite"

    def __init__(
        self,
        db_path: str | Path,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        super().__init__(timeout=timeout, max_rows=max_rows)
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        # file: URI 加 mode=ro：驱动层强制只读，即便 guard 漏了写操作也执行不了。
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=self.timeout)
        # BIRD 的数据库里有非 UTF-8 的脏字节，默认解码会直接抛异常，
        # 这里退化成替换字符，让查询能跑完。
        conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
        return conn

    def _fetch(self, sql: str, n: int) -> tuple[list[str], list[tuple[Any, ...]]]:
        if not self.db_path.exists():
            raise QueryFailed(f"数据库不存在：{self.db_path}")
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise QueryFailed(f"打开数据库失败：{exc}") from exc

        # 超时靠另一个线程调 interrupt()：SQLite 的 timeout 参数只管锁等待，
        # 管不住一条跑飞了的查询。
        timer = threading.Timer(self.timeout, conn.interrupt)
        timer.daemon = True
        timer.start()
        try:
            cur = conn.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            return columns, cur.fetchmany(n)
        except sqlite3.OperationalError as exc:
            msg = str(exc)
            if "interrupted" in msg.lower():
                msg = f"查询超时（超过 {self.timeout:.0f} 秒被中断）"
            raise QueryFailed(msg) from exc
        except sqlite3.Error as exc:
            raise QueryFailed(f"{type(exc).__name__}: {exc}") from exc
        finally:
            timer.cancel()
            conn.close()
