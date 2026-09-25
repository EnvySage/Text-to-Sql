"""评测集加载。

目前支持 BIRD 的 dev 集目录结构：

    <root>/dev.json                      题目列表
    <root>/dev_databases/<db_id>/<db_id>.sqlite

也支持在 PostgreSQL 上跑（BIRD Mini-Dev 的 PG 版）：给 ``pg_dsn`` 时不找 .sqlite 文件，
每道题的 ``db`` 是带 ``search_path=<db_id>`` 的连接地址，见 ``eval/pg/``。

各家发布包的目录层级偶有出入（有的多一层 ``dev_20240627/``），
所以数据库路径用搜索而不是硬拼，找不到就明确报错，不静默跳过——
静默跳过会让准确率的分母悄悄变小。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sandbox.postgres import with_search_path


@dataclass(slots=True)
class Item:
    """一道题。

    ``evidence`` 是 BIRD 特有的业务口径说明（例如"复购指同一客户下单超过一次"）。
    真实业务里这类知识也确实不在数据库里，而在文档里——这正是检索层后面要解决的。
    """

    qid: str
    db_id: str
    question: str
    gold_sql: str
    evidence: str = ""
    difficulty: str = ""
    # SQLite 文件路径，或 PostgreSQL 连接地址；交给 sandbox.open_sandbox 按类型选执行器
    db: Path | str | None = None


@lru_cache(maxsize=64)
def _db_index(root: Path) -> dict[str, Path]:
    """把 root 下所有 .sqlite/.db 文件按库名建索引。"""
    index: dict[str, Path] = {}
    for pattern in ("**/*.sqlite", "**/*.db"):
        for p in root.glob(pattern):
            index.setdefault(p.stem, p)
    return index


def load_bird(
    root: str | Path,
    *,
    limit: int | None = None,
    seed: int = 0,
    questions_file: str | None = None,
    pg_dsn: str | None = None,
) -> list[Item]:
    """加载 BIRD 风格的数据集。

    ``limit`` 走固定种子的随机抽样，不是取前 N 条：BIRD 的题目按库聚在一起，
    取前 N 条等于只评测了两三个数据库，得出的准确率没有代表性。
    """
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"数据集目录不存在：{root}")

    if questions_file:
        qpath = root / questions_file
    else:
        found = [p for p in root.glob("**/*.json") if "dev" in p.name.lower()]
        if not found:
            raise FileNotFoundError(f"在 {root} 下没找到题目 json 文件")
        # 取最短路径的那个，避免选到嵌套目录里的副本
        qpath = min(found, key=lambda p: len(p.parts))

    raw = json.loads(qpath.read_text(encoding="utf-8"))
    index = {} if pg_dsn else _db_index(root)

    items: list[Item] = []
    missing: set[str] = set()
    for i, r in enumerate(raw):
        db_id = r.get("db_id", "")
        if pg_dsn:
            db: Path | str | None = with_search_path(pg_dsn, db_id)
        else:
            db = index.get(db_id)
            if db is None:
                missing.add(db_id)
        items.append(
            Item(
                qid=str(r.get("question_id", i)),
                db_id=db_id,
                question=r.get("question", ""),
                gold_sql=r.get("SQL") or r.get("query") or "",
                evidence=r.get("evidence", "") or "",
                difficulty=r.get("difficulty", "") or "",
                db=db,
            )
        )

    if missing:
        raise FileNotFoundError(
            f"有 {len(missing)} 个数据库文件找不到，例如：{sorted(missing)[:5]}。"
            f"检查数据库是否解压到了 {root} 下。"
        )

    if limit is not None and limit < len(items):
        rng = random.Random(seed)
        items = rng.sample(items, limit)
        items.sort(key=lambda x: (x.db_id, x.qid))

    return items
