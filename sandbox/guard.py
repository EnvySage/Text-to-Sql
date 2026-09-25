"""SQL 安全闸门：用 AST 判断而不是正则匹配。

正则挡不住的例子很多：注释混淆（``SEL/**/ECT``）、多语句拼接
（``SELECT 1; DROP TABLE t``）、大小写和空白变形。这里用 sqlglot 解析成语法树，
只放行确定安全的节点类型，其余一律拒绝——白名单，不是黑名单。

这层是给 LLM 生成的 SQL 用的。模型不是攻击者，但它会在幻觉下写出
``DELETE FROM orders``，而数据库连接如果碰巧有写权限，那就是真删了。
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

# 允许作为顶层语句的表达式类型。只有查询。
_ALLOWED_ROOT = (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)

# 出现即拒绝的节点类型。即使嵌在子查询里也不行。
# Into：PG 的 SELECT INTO 等于建表，MySQL 的 INTO OUTFILE 写服务器文件；
# Lock：FOR UPDATE / FOR SHARE 会加行锁，能卡住线上写入。
_FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.TruncateTable, exp.Merge, exp.Grant, exp.Attach, exp.Detach,
    exp.Set, exp.Pragma, exp.Command, exp.Into, exp.Lock,
)

# 披着 SELECT 外衣的危险函数：读写服务器文件、改库、挂起或杀连接、
# 改会话配置（能把只读关掉）、把 SQL 字符串交给函数执行（绕过 AST 检查）。
#
# 按方言分组只是为了好读，检查时全部生效：dialect 一旦传错，
# 按方言挑选的黑名单就会留出漏洞；而这些名字在别的方言里也不是合法的分析函数。
#
# 黑名单不可能列全——服务端库的自定义函数同样可以有写副作用。
# 所以服务端库上连接层的只读账号是必需的，不是兜底。
_FORBIDDEN_FUNCS = {
    # SQLite
    "readfile", "writefile", "load_extension", "edit", "fts3_tokenizer",
    # PostgreSQL
    "nextval", "setval", "set_config",
    "pg_stat_file", "pg_terminate_backend", "pg_cancel_backend",
    "pg_reload_conf", "pg_rotate_logfile", "pg_notify",
    "query_to_xml", "query_to_xml_and_xmlschema", "query_to_xmlschema",
    # MySQL
    "load_file", "sleep", "benchmark",
    "get_lock", "release_lock", "release_all_locks",
    "master_pos_wait", "source_pos_wait", "sys_exec", "sys_eval",
}

# 同一族函数变体多，按前缀挡。前缀都带下划线，``lower``、``log`` 不受影响。
_FORBIDDEN_FUNC_PREFIXES = (
    "pg_read_", "pg_ls_", "pg_sleep", "pg_advisory", "pg_try_advisory",
    "lo_", "dblink",
)


def _forbidden_func(name: str) -> bool:
    name = name.lower()
    return name in _FORBIDDEN_FUNCS or name.startswith(_FORBIDDEN_FUNC_PREFIXES)


@dataclass(slots=True)
class GuardResult:
    ok: bool
    reason: str = ""
    normalized: str = ""


def check(sql: str, dialect: str = "sqlite") -> GuardResult:
    """校验一条 SQL 是否只读且单语句。

    返回 ``GuardResult``，不抛异常——拒绝原因需要作为工具结果回灌给模型，
    让它自己改写，而不是中断整轮。
    """
    text = (sql or "").strip()
    if not text:
        return GuardResult(False, "SQL 为空")

    try:
        statements = sqlglot.parse(text, dialect=dialect)
    except Exception as exc:
        return GuardResult(False, f"SQL 无法解析：{exc}")

    statements = [s for s in statements if s is not None]
    if not statements:
        return GuardResult(False, "没有解析出任何语句")
    if len(statements) > 1:
        return GuardResult(
            False, f"只允许单条语句，检测到 {len(statements)} 条（禁止用分号拼接）"
        )

    root = statements[0]

    # WITH ... SELECT 的顶层节点是被查询包裹的，取出真正的主体判断。
    body = root
    if isinstance(root, exp.With):
        body = root.this

    if not isinstance(body, _ALLOWED_ROOT):
        return GuardResult(
            False, f"只允许 SELECT 查询，检测到 {type(body).__name__.upper()}"
        )

    for node in root.walk():
        if isinstance(node, _FORBIDDEN):
            return GuardResult(
                False, f"禁止的操作：{type(node).__name__.upper()}"
            )
        if isinstance(node, exp.Anonymous):
            name = node.this or ""
            if _forbidden_func(name):
                return GuardResult(False, f"禁止的函数：{name.lower()}")

    return GuardResult(True, normalized=root.sql(dialect=dialect))


def ensure_limit(sql: str, limit: int, dialect: str = "sqlite") -> str:
    """给没有 LIMIT 的查询补一个上限。

    这是成本护栏不是安全护栏：模型写出 ``SELECT * FROM orders`` 时，
    几百万行传回来会撑爆 context，而不是撑爆数据库。
    已经有 LIMIT 的不动——模型可能故意要 top-N。
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return sql
    if tree is None:
        return sql

    body = tree.this if isinstance(tree, exp.With) else tree
    if isinstance(body, _ALLOWED_ROOT) and not body.args.get("limit"):
        return tree.limit(limit).sql(dialect=dialect)
    return sql
