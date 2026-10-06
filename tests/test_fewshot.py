"""BM25 few-shot 检索：排序、库过滤、零分剔除。"""

from __future__ import annotations

from retrieval.fewshot import BM25Index, Example, format_examples, tokenize

CORPUS = [
    Example("How many molecules are carcinogenic?", "SELECT COUNT(*) FROM molecule", "toxicology"),
    Example("What is the average writing score of the schools?", "SELECT AVG(score)", "california_schools"),
    Example("How many cards are in the set?", "SELECT COUNT(*) FROM cards", "card_games"),
]


def test_tokenize_lowercases_and_splits():
    assert tokenize("How many CARDS, in the set?") == ["how", "many", "cards", "in", "the", "set"]


def test_ranks_the_most_similar_question_first():
    idx = BM25Index(CORPUS)
    top = idx.top_k("How many carcinogenic molecules are there?", 1)
    assert top and top[0].db_id == "toxicology"


def test_db_filter_restricts_candidates():
    idx = BM25Index(CORPUS)
    top = idx.top_k("How many molecules are carcinogenic?", 3, db_id="card_games")
    assert all(e.db_id == "card_games" for e in top)


def test_unrelated_query_returns_nothing():
    """一条词都不重叠的示例没有参考价值，不该凑数污染 prompt。"""
    idx = BM25Index(CORPUS)
    assert idx.top_k("zzz qqq", 3) == []


def test_k_zero_returns_empty():
    idx = BM25Index(CORPUS)
    assert idx.top_k("How many molecules are carcinogenic?", 0) == []


def test_empty_corpus_is_safe():
    idx = BM25Index([])
    assert idx.top_k("anything", 3) == []


def test_ties_break_by_corpus_order_for_reproducibility():
    idx = BM25Index([Example("same text", "SELECT 1", "a"), Example("same text", "SELECT 2", "a")])
    assert [e.sql for e in idx.top_k("same text", 2)] == ["SELECT 1", "SELECT 2"]


def test_format_examples_empty_is_empty_string():
    assert format_examples([]) == ""


def test_format_examples_contains_question_and_sql():
    s = format_examples([Example("有多少行？", "SELECT COUNT(*) FROM t", "db")])
    assert "有多少行？" in s and "SELECT COUNT(*) FROM t" in s
