"""执行准确率的比对逻辑。

这是整个评测里最容易写错、错了又最难发现的一块：比对太松，准确率虚高，
后面所有消融数字都是假的；比对太严，真正答对的被判错，优化方向会被带偏。

采用的口径（和 BIRD 官方基本一致，差异已标注）：

- **行顺序不敏感**：除非题目明确要求排序，SQL 不保证返回顺序。
- **行重复敏感**：用多重集而不是集合比较。官方用 ``set()``，会把
  ``[(1,), (1,)]`` 和 ``[(1,)]`` 判成相等——那是 GROUP BY 写错的典型症状，
  不该放过。这是本项目**比官方更严**的一处，必须在报告里写明。
- **列顺序敏感**：``SELECT name, age`` 和 ``SELECT age, name`` 不算同一个答案。
- **浮点按精度比**：避免 ``0.30000000000000004`` 这类浮点误差误判。
- **数值跨类型相等**：``1`` 和 ``1.0`` 视为相同，SQLite 的类型亲和性经常
  让同一个值在两条 SQL 里返回不同的 Python 类型。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Sequence

FLOAT_PRECISION = 4


def _norm_cell(v: Any, precision: int = FLOAT_PRECISION) -> Any:
    """把一个单元格压成可哈希、可比较的规范形式。"""
    if v is None:
        return None
    if isinstance(v, bool):
        # SQLite 没有布尔类型，用 0/1 存。所以 True 和 1 本来就是同一个值，
        # 这里显式转成数值，不依赖 "bool 是 int 子类" 这种隐式行为。
        return float(v)
    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, (int, float)):
        f = float(v)
        if f != f:  # NaN
            return "NaN"
        # 统一成 float 再取整：让 1 和 1.0 落到同一个值上
        return round(f, precision)
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace").strip()
    if isinstance(v, str):
        return v.strip()
    return str(v)


def _norm_rows(
    rows: Iterable[Sequence[Any]], precision: int = FLOAT_PRECISION
) -> list[tuple[Any, ...]]:
    return [tuple(_norm_cell(c, precision) for c in row) for row in rows]


@dataclass(slots=True)
class MatchResult:
    match: bool
    reason: str = ""


def result_match(
    pred_rows: Iterable[Sequence[Any]],
    gold_rows: Iterable[Sequence[Any]],
    *,
    order_sensitive: bool = False,
    precision: int = FLOAT_PRECISION,
) -> MatchResult:
    """比较两个结果集。

    ``order_sensitive`` 只在题目要求排序时打开。默认关闭，因为不带 ORDER BY
    的 SQL 返回顺序由执行计划决定，拿顺序判对错不合理。
    """
    pred = _norm_rows(pred_rows, precision)
    gold = _norm_rows(gold_rows, precision)

    if not pred and not gold:
        return MatchResult(True, "双方都是空结果")
    if len(pred) != len(gold):
        return MatchResult(False, f"行数不同：预测 {len(pred)}，标准 {len(gold)}")

    pw = len(pred[0]) if pred else 0
    gw = len(gold[0]) if gold else 0
    if pw != gw:
        return MatchResult(False, f"列数不同：预测 {pw}，标准 {gw}")

    if order_sensitive:
        if pred == gold:
            return MatchResult(True)
        return MatchResult(False, "行顺序或内容不一致（该题按顺序敏感比对）")

    if Counter(pred) == Counter(gold):
        return MatchResult(True)

    # 给出一个具体的差异样例，便于人工复查评测本身是否合理
    missing = (Counter(gold) - Counter(pred)).most_common(1)
    extra = (Counter(pred) - Counter(gold)).most_common(1)
    bits = []
    if missing:
        bits.append(f"缺少 {missing[0][0]}")
    if extra:
        bits.append(f"多出 {extra[0][0]}")
    return MatchResult(False, "内容不一致：" + "；".join(bits))


# 题干里出现这些表达时，答案的顺序本身是题目的一部分。
#
# 必须按词边界匹配，不能用子串包含。BIRD 里 orders 表随处可见，
# 子串匹配会把 "How many orders are there?" 判成排序题，
# 导致一大批本该答对的题被按顺序比对而误判。
_ORDER_PATTERN = re.compile(
    r"\b(?:"
    r"ordered\s+by|sorted\s+by|sort\s+by|order\s+by"
    r"|in\s+(?:ascending|descending)(?:\s+order)?"
    r"|ascending|descending"
    r"|top\s+\d+|first\s+\d+|last\s+\d+"
    r"|highest|lowest|largest|smallest|maximum|minimum"
    r"|ranked|ranking"
    r")\b",
    re.IGNORECASE,
)


def needs_order(question: str) -> bool:
    """粗略判断题目是否要求结果有序。

    是启发式，不是精确判断。宁可漏判也不要误判：误判成顺序敏感会把本来
    正确的答案判错，漏判只是稍微放松了标准。
    """
    return bool(_ORDER_PATTERN.search(question or ""))
