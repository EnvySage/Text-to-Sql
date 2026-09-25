"""比对逻辑的校验。

每个测试对应一个"比对写错了就会让准确率变成假数字"的场景。
"""

from __future__ import annotations

from eval.metrics import needs_order, result_match


def test_identical_results_match():
    assert result_match([(1, "a")], [(1, "a")]).match


def test_row_order_is_ignored_by_default():
    """不带 ORDER BY 的查询，返回顺序由执行计划决定，不能据此判错。"""
    assert result_match([(1,), (2,)], [(2,), (1,)]).match


def test_row_order_matters_when_asked():
    assert not result_match([(1,), (2,)], [(2,), (1,)], order_sensitive=True).match


def test_duplicate_rows_are_not_collapsed():
    """本项目比 BIRD 官方更严的一处：官方用 set()，会放过 GROUP BY 写错。"""
    assert not result_match([(1,), (1,)], [(1,)]).match


def test_int_and_float_are_the_same_value():
    """SQLite 的类型亲和性会让同一个值返回不同 Python 类型。"""
    assert result_match([(1,)], [(1.0,)]).match


def test_float_noise_within_precision_matches():
    assert result_match([(0.30000000000000004,)], [(0.3,)]).match


def test_float_difference_beyond_precision_fails():
    assert not result_match([(0.3001,)], [(0.3,)]).match


def test_bool_equals_its_integer_form():
    """SQLite 用 0/1 存布尔，True 和 1 是同一个值，必须判相等。"""
    assert result_match([(True,)], [(1,)]).match
    assert not result_match([(True,)], [(0,)]).match


def test_null_matches_null_but_not_zero():
    assert result_match([(None,)], [(None,)]).match
    assert not result_match([(None,)], [(0,)]).match


def test_whitespace_around_strings_is_ignored():
    assert result_match([(" Alice ",)], [("Alice",)]).match


def test_column_order_matters():
    """SELECT name, age 和 SELECT age, name 不是同一个答案。"""
    assert not result_match([("a", 1)], [(1, "a")]).match


def test_column_count_mismatch_is_reported():
    r = result_match([(1, 2)], [(1,)])
    assert not r.match and "列数不同" in r.reason


def test_row_count_mismatch_is_reported():
    r = result_match([(1,)], [(1,), (2,)])
    assert not r.match and "行数不同" in r.reason


def test_both_empty_counts_as_match():
    assert result_match([], []).match


def test_mismatch_reason_names_a_concrete_difference():
    r = result_match([(1,)], [(2,)])
    assert not r.match and ("缺少" in r.reason or "多出" in r.reason)


def test_order_hint_detection():
    assert needs_order("List the top 3 customers by revenue")
    assert needs_order("Show products ordered by price descending")
    assert needs_order("Which customer has the highest total amount?")


def test_order_hint_does_not_fire_on_the_word_orders():
    """BIRD 里 orders 表随处可见；子串匹配会把普通计数题误判成排序题。"""
    assert not needs_order("How many orders are there?")
    assert not needs_order("List all orders placed in 2023")
    assert not needs_order("What is the average order amount?")
