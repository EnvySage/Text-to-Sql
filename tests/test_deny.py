"""禁查列：按词边界匹配，不能按子串。

子串匹配会把 metadata 当成 data、content_hit_count 当成 content 拦下来——
实测踩过：模型被拦后花了好几轮去猜"为什么 metadata 也被禁"，最后撞上限没收敛。
"""

from __future__ import annotations

from sandbox.base import ExecResult
from sandbox.deny import DenyColumns


class FakeSandbox:
    dialect = "postgres"

    def __init__(self) -> None:
        self.ran: list[str] = []

    def run(self, sql: str, *, enforce_limit: bool = True) -> ExecResult:
        self.ran.append(sql)
        return ExecResult(ok=True, columns=["x"], rows=[(1,)])


def _denied(sql: str, cols=("content", "data")) -> bool:
    return not DenyColumns(FakeSandbox(), cols).run(sql).ok


def test_denied_column_is_blocked():
    assert _denied("SELECT data FROM vault_blobs")
    assert _denied("SELECT content FROM records")


def test_quoted_identifier_is_blocked():
    assert _denied('SELECT "data" FROM vault_blobs')


def test_substring_of_a_denied_name_is_not_blocked():
    """metadata 含 data、content_hit_count 含 content——都不是那一列。"""
    assert not _denied("SELECT metadata FROM chunks")
    assert not _denied("SELECT content_hit_count FROM user_terms")


def test_column_inside_a_longer_word_is_not_blocked():
    assert not _denied("SELECT u.metadata->>'x' FROM chunks u")


def test_allowed_sql_reaches_the_wrapped_sandbox():
    inner = FakeSandbox()
    DenyColumns(inner, ["content"]).run("SELECT count(*) FROM records")
    assert inner.ran == ["SELECT count(*) FROM records"]


def test_blocked_sql_never_reaches_the_sandbox():
    inner = FakeSandbox()
    DenyColumns(inner, ["content"]).run("SELECT content FROM records")
    assert inner.ran == []


def test_empty_list_is_a_passthrough():
    inner = FakeSandbox()
    DenyColumns(inner, []).run("SELECT content FROM records")
    assert inner.ran == ["SELECT content FROM records"]
