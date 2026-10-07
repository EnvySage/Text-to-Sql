"""Streamlit 界面：把 agent 干活的过程展示出来。

重点是**执行过程**——它先想了什么、试跑了什么、看到什么、怎么改的。
不做成聊天框：聊天框看不出 agent 做了什么，和普通 ChatGPT 没区别。

    uv run streamlit run web/app.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from agent import core
from agent.schema import schema_text
from llm.router import Router
from sandbox import open_sandbox
from sandbox.deny import DenyColumns

# Streamlit 每次交互都重跑整个脚本，load_dotenv 是幂等的，重复调没有副作用。
# 不加载的话 Router 找不到 $LOCAL_API_KEY 会直接抛错。
load_dotenv()

st.set_page_config(page_title="数据分析 agent", page_icon="🔎", layout="wide")

DEFAULT_DB = "eval/datasets/bird/dev_20240627/dev_databases/california_schools/california_schools.sqlite"
DEFAULT_DENY = (
    "content,embedding,password_hash,api_key,data,"
    "mood_analysis,learning_analysis,todo_analysis,rhythm_analysis,overall_summary"
)

SENSITIVE_HINT = "含隐私数据或会爆上下文的列。按子串匹配，别放太泛的词。"


def _table(preview: str):
    """把 ``ExecResult.to_markdown()`` 的输出还原成表格。解析不了就返回 None。"""
    lines = [ln for ln in (preview or "").splitlines() if ln.strip()]
    if len(lines) < 1:
        return None
    head = [c.strip() for c in lines[0].split("|")]
    rows, notes = [], []
    for ln in lines[1:]:
        if ln.startswith("...") or ln.startswith("（"):
            notes.append(ln)
            continue
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) != len(head):
            return None
        rows.append(cells)
    return head, rows, notes


def _render_tool_result(p: dict) -> None:
    if not p.get("ok"):
        st.error(f"← 失败：{p.get('error', '')}")
        return
    st.success(f"← {p.get('rows', 0)} 行")
    parsed = _table(p.get("preview", ""))
    if parsed:
        head, rows, notes = parsed
        st.dataframe(
            [dict(zip(head, r)) for r in rows], use_container_width=True, hide_index=True
        )
        for n in notes:
            st.caption(n)
    else:
        st.code(p.get("preview", ""), language="text")


def _render_events(events, *, show_thinking: bool) -> None:
    step = 0
    for e in events:
        p = e.payload
        if e.type == "step_start":
            step = p.get("step", step)
            st.markdown(f"#### 第 {step} 步")
            if show_thinking and (p.get("reasoning") or "").strip():
                with st.expander("💭 思考", expanded=False):
                    st.markdown(p["reasoning"])
            if (p.get("text") or "").strip():
                st.markdown(p["text"])
        elif e.type == "tool_call":
            st.markdown(f"**▸ {p.get('name', '')}**")
            sql = (p.get("args") or {}).get("sql")
            if sql:
                st.code(str(sql), language="sql")
        elif e.type == "tool_result":
            _render_tool_result(p)
        elif e.type == "error":
            st.error(p.get("message", ""))


def _cost(out: core.AgentOutcome, wall: float) -> None:
    u = out.usage
    cols = st.columns(5)
    cols[0].metric("步数", out.steps)
    cols[1].metric("工具调用", out.tool_calls)
    cols[2].metric("耗时", f"{wall:.1f}s")
    cols[3].metric("输入 token", f"{u.input_tokens:,}")
    cols[4].metric("输出 token", f"{u.output_tokens:,}")
    if out.hit_cap:
        st.warning("撞到步数上限，没收敛——这是异常信号，不是「用满了额度」")


# ── 侧边栏 ────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🔎 数据分析 agent")
    db = st.text_input("数据库", value=DEFAULT_DB, help="SQLite 文件路径，或 postgresql:// 连接地址")
    deny_raw = st.text_area("禁查列", value="", height=90, help=SENSITIVE_HINT)
    max_steps = st.number_input("步数上限", 1, 30, core.MAX_STEPS_DEFAULT,
                                help="防跑飞的兜底，不是预算")
    show_thinking = st.toggle("显示思考", value=True)
    st.divider()
    st.caption("演示用：先跑一次并勾选「存轨迹」，之后可以重放——不连模型、不花钱、结果稳定。")
    save_trace = st.checkbox("存轨迹", value=False)

deny = [c for c in deny_raw.split(",") if c.strip()]

# ── 主区 ──────────────────────────────────────────────────────────────────

question = st.text_input("问一句", placeholder="例：每个月分别有多少条记录？按月份倒序列出")

run = st.button("运行", type="primary", disabled=not (db and question))

if run:
    try:
        with st.spinner("连库、读 schema…"):
            sandbox = open_sandbox(db)
            if deny:
                sandbox = DenyColumns(sandbox, deny)
            schema = schema_text(db)
            router = Router.from_file()
            provider = router.for_role("sql_gen")
        with st.spinner(f"跑循环（{provider.model}）…"):
            started = time.perf_counter()
            events = list(core.run(
                question, provider=provider, sandbox=sandbox, schema=schema,
                dialect=sandbox.dialect, max_steps=max_steps,
                max_tokens=router.max_tokens_for("sql_gen"),
            ))
            wall = time.perf_counter() - started
        out = core.consume(iter(events))
        st.session_state.trace = {
            "question": question, "db": str(db), "dialect": sandbox.dialect,
            "events": [{"type": e.type, "payload": e.payload} for e in events],
            "final_sql": out.sql,
            "cost": {
                "steps": out.steps, "tool_calls": out.tool_calls, "wall": wall,
                "input": out.usage.input_tokens, "output": out.usage.output_tokens,
                "reasoning": out.usage.reasoning_tokens, "hit_cap": out.hit_cap,
            },
            "deny": deny,
        }
    except Exception as exc:  # 连不上库、schema 读不出来等，直接显示给人看
        st.error(f"跑不起来：{type(exc).__name__}: {exc}")

trace = st.session_state.get("trace")

if trace:
    st.divider()
    st.caption(f"{trace['db']} · {trace.get('dialect', '')}"
               + (f" · 禁查列：{', '.join(trace['deny'])}" if trace.get("deny") else ""))

    _render_events(
        [core.AgentEvent(e["type"], e.get("payload") or {}) for e in trace["events"]],
        show_thinking=show_thinking,
    )

    st.divider()
    st.subheader("最终答案")
    if trace["final_sql"]:
        st.code(trace["final_sql"], language="sql")
    else:
        st.warning("没有产出 SQL")

    c = trace.get("cost") or {}
    if c:
        st.subheader("这一问的账")
        _cost(
            core.AgentOutcome(
                sql=trace["final_sql"], steps=c.get("steps", 0),
                tool_calls=c.get("tool_calls", 0), hit_cap=c.get("hit_cap", False),
                usage=core.Usage(input_tokens=c.get("input", 0),
                                 output_tokens=c.get("output", 0),
                                 reasoning_tokens=c.get("reasoning", 0)),
            ),
            c.get("wall", 0.0),
        )

    if save_trace:
        path = Path("trace.json")
        path.write_text(json.dumps(trace, ensure_ascii=False, indent=1), encoding="utf-8")
        st.caption(f"已存到 {path.resolve()}——命令行 `--replay` 可以重放")
