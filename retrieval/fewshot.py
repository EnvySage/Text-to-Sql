"""BM25 few-shot 检索：从「问题 + 标准 SQL」配对里挑最像当前问题的几条，拼进 prompt。

纯 Python 实现，不引第三方依赖——BM25 本身就是词频统计，够用，也不需要索引落盘。

**检索限定在同一个库**：示例里的表名要真在当前 schema 里。跨库示例会把模型引到
不存在的表上，那不是 few-shot，是干扰。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

# 小写后按非字母数字切分。BIRD 的问题是英文，不需要中文分词。
_TOKEN = re.compile(r"[a-z0-9_]+")

# BM25 的两个自由参数，取文献里的常用默认值，没有为本数据集调过。
K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


@dataclass(slots=True)
class Example:
    """一条 few-shot 示例。

    ``evidence`` 是 BIRD 的业务口径说明。带上它，模型能看到"业务词 → 列"是怎么映射的——
    而"选错列"正是错题里最大的一类（见 EVAL 的 fs 明细）。
    """

    question: str
    sql: str
    db_id: str = ""
    evidence: str = ""


def format_examples(examples: list[Example]) -> str:
    """把示例拼成给模型看的参考段。空列表返回空串。"""
    if not examples:
        return ""
    blocks = []
    for e in examples:
        body = f"问题：{e.question}"
        if e.evidence.strip():
            body += f"\n业务口径：{e.evidence.strip()}"
        body += f"\nSQL：\n{e.sql}"
        blocks.append(body)
    joined = "\n\n".join(blocks)
    return f"相似问题与标准答案（写法参考，问题不同，不要照抄）：\n\n{joined}\n"


class BM25Index:
    """对一个 (问题, SQL) 语料建 BM25 索引。

    只索引问题文本——SQL 是拿来给模型看的，不参与相似度计算。
    """

    def __init__(self, examples: list[Example], *, k1: float = K1, b: float = B) -> None:
        self.k1 = k1
        self.b = b
        self.examples = list(examples)
        self._freqs = [Counter(tokenize(e.question)) for e in self.examples]
        self._lens = [sum(f.values()) for f in self._freqs]
        self._avgdl = (sum(self._lens) / len(self._lens)) if self._lens else 0.0

        doc_freq: Counter[str] = Counter()
        for f in self._freqs:
            doc_freq.update(f.keys())
        n = len(self._freqs)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in doc_freq.items()}

    def top_k(self, query: str, k: int, *, db_id: str | None = None) -> list[Example]:
        """返回最相似的 k 条。

        ``db_id`` 给定时只在该库的示例里找。得分为 0 的不返回——一条词都不重叠的
        示例对模型没有参考价值，凑数只会污染 prompt。
        """
        if k <= 0:
            return []
        terms = tokenize(query)
        scored: list[tuple[float, int]] = []
        for i, e in enumerate(self.examples):
            if db_id is not None and e.db_id != db_id:
                continue
            scored.append((self._score(terms, i), i))
        # 同分时按语料顺序，保证结果可复现
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [self.examples[i] for s, i in scored[:k] if s > 0]

    def _score(self, terms: list[str], i: int) -> float:
        freq, dl = self._freqs[i], self._lens[i]
        if not dl or not self._avgdl:
            return 0.0
        total = 0.0
        for t in terms:
            tf = freq.get(t)
            if not tf:
                continue
            denom = tf + self.k1 * (1 - self.b + self.b * dl / self._avgdl)
            total += self._idf.get(t, 0.0) * tf * (self.k1 + 1) / denom
        return total
