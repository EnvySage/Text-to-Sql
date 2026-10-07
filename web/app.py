"""Streamlit 界面：把 agent 干活的过程展示出来。

**为什么 agent 跑在后台线程里**：Streamlit 每次交互（点按钮、填输入框）都会把整个脚本
从头重跑一遍，而 agent 循环要跑十几秒到几十秒、还可能中途停下来问用户。所以循环放在
线程里，主脚本只负责两件事——把进度画出来、把用户的话送回去。两者通过队列通信。

    uv run streamlit run web/app.py
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from agent import core
from agent.schema import schema_text
from llm.router import Router
from sandbox import open_sandbox
from sandbox.deny import DenyColumns

# Streamlit 每次交互都重跑整个脚本，load_dotenv 幂等，重复调没有副作用。
load_dotenv()

st.set_page_config(page_title="数据分析 agent", page_icon="🔎", layout="wide")

DEFAULT_DB = "eval/datasets/bird/dev_20240627/dev_databases/california_schools/california_schools.sqlite"
DEFAULT_DENY = (
    "content,embedding,password_hash,api_key,data,"
    "mood_analysis,learning_analysis,todo_analysis,rhythm_analysis,overall_summary"
)
TERMS_FILE = Path("terms.json")


# ── 术语表 ────────────────────────────────────────────────────────────────

def _load_terms() -> dict[str, str]:
    if not TERMS_FILE.exists():
        return {}
    try:
        data = json.loads(TERMS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _terms_for(question: str, terms: dict[str, str]) -> str:
    """只带**问题里出现过**的术语——整表塞进去只会稀释注意力。"""
    hits = [f"- {t}：{d}" for t, d in terms.items() if t and t in question]
    return "已确认的业务口径：\n" + "\n".join(hits) if hits else ""


# ── 后台运行 ──────────────────────────────────────────────────────────────

class AgentRun:
    """一次运行。线程跑循环，主脚本看进度、回答问题。"""

    def __init__(self, question: str, db: str, deny: list[str],
                 max_steps: int, terms: dict[str, str]) -> None:
        self.question, self.db, self.deny = question, db, deny
        self.max_steps, self.terms = max_steps, terms

        self.events: list[core.AgentEvent] = []
        self.pending: dict | None = None      # 待用户回答的问题
        self.done = False
        self.error = ""
        self.result: dict = {}

        self._answer: str | None = None
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)

    # -- 主脚本用 --

    def snapshot(self) -> tuple[list, dict | None, bool, str, dict]:
        with self._lock:
            return list(self.events), self.pending, self.done, self.error, dict(self.result)

    def answer(self, text: str) -> None:
        with self._lock:
            self._answer = text

    # -- 后台线程用 --

    def _ask(self, args: dict) -> str:
        with self._lock:
            self.pending = args
            self._answer = None
        while True:                       # 等主脚本把回答塞回来
            time.sleep(0.2)
            with self._lock:
                if self._answer is not None:
                    text, self._answer, self.pending = self._answer, None, None
                    return text

    def _run(self) -> None:
        try:
            sandbox = open_sandbox(self.db)
            if self.deny:
                sandbox = DenyColumns(sandbox, self.deny)
            schema = schema_text(self.db)
            router = Router.from_file()
            provider = router.for_role("sql_gen")
            for e in core.run(
                self.question, provider=provider, sandbox=sandbox, schema=schema,
                dialect=sandbox.dialect, evidence=_terms_for(self.question, self.terms),
                max_steps=self.max_steps, max_tokens=router.max_tokens_for("sql_gen"),
                ask=self._ask,
            ):
                with self._lock:
                    self.events.append(e)

            out = core.consume(iter(self.events))
            res = sandbox.run(out.sql) if out.sql else None
            preview = res.to_markdown(max_rows=20) if (res is not None and res.ok) else ""
            conclusion, _ = core.conclude(self.question, out.sql, preview, provider=provider)
            with self._lock:
                self.result = {
                    "sql": out.sql, "preview": preview, "conclusion": conclusion,
                    "dialect": sandbox.dialect, "hit_cap": out.hit_cap,
                    "steps": out.steps, "tool_calls": out.tool_calls,
                    "input": out.usage.input_tokens, "output": out.usage.output_tokens,
                    "reasoning": out.usage.reasoning_tokens,
                }
        except Exception as exc:          # 连不上库、schema 读不出来等，显示给人看
            with self._lock:
                self.error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self.done = True


# ── 渲染 ──────────────────────────────────────────────────────────────────

def _table(preview: str):
    """把 ``to_markdown()`` 的输出还原成表格。解析不了就返回 None。"""
    lines = [ln for ln in (preview or "").splitlines() if ln.strip()]
    if not lines:
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


def _show_table(preview: str) -> None:
    parsed = _table(preview)
    if parsed:
        head, rows, notes = parsed
        st.dataframe([dict(zip(head, r)) for r in rows],
                     use_container_width=True, hide_index=True)
        for n in notes:
            st.caption(n)
    else:
        st.code(preview or "（空）", language="text")


def _render_timeline(events, *, show_thinking: bool) -> None:
    """把整段过程画出来。跑完或重放时用——运行中只显示进度，避免每 0.5 秒重建整页。"""
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
            name = str(p.get("name", ""))
            args = p.get("args") or {}
            if name == "ask_user":
                st.info(f"❓ 问用户：{args.get('question', '')}")
                cands = args.get("candidates") or []
                for i, c in enumerate(cands, 1):
                    st.caption(f"　{i}. {c}")
            else:
                st.markdown(f"**▸ {name}**")
                if args.get("sql"):
                    st.code(str(args["sql"]), language="sql")
        elif e.type == "tool_result":
            if p.get("ok"):
                st.success(f"← {p.get('rows', 0)} 行")
                if (p.get("preview") or "").strip():
                    _show_table(p["preview"])
            else:
                st.error(f"← 失败：{p.get('error', '')}")
        elif e.type == "error":
            st.error(p.get("message", ""))


def _render_result(result: dict) -> None:
    st.divider()
    st.subheader("最终答案")
    if result.get("sql"):
        st.code(result["sql"], language="sql")
    else:
        st.warning("没有产出 SQL")
    if result.get("preview"):
        _show_table(result["preview"])
    if result.get("conclusion"):
        st.subheader("结论")
        st.success(result["conclusion"])
    if result.get("steps"):
        st.subheader("这一问的账")
        c = st.columns(5)
        c[0].metric("步数", result.get("steps", 0))
        c[1].metric("工具调用", result.get("tool_calls", 0))
        c[2].metric("输入 token", f"{result.get('input', 0):,}")
        c[3].metric("输出 token", f"{result.get('output', 0):,}")
        c[4].metric("思考 token", f"{result.get('reasoning', 0):,}")
    if result.get("hit_cap"):
        st.warning("撞到步数上限，没收敛——最后那条 SQL 是逼出来的，未必可靠")


# ── 侧边栏 ────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🔎 数据分析 agent")
    db = st.text_input("数据库", value=DEFAULT_DB,
                       help="SQLite 文件路径，或 postgresql:// 连接地址")
    deny_raw = st.text_area("禁查列", value="", height=80,
                            help="含隐私数据或会爆上下文的列。按词边界匹配。")
    max_steps = st.number_input("步数上限", 1, 30, core.MAX_STEPS_DEFAULT,
                                help="防跑飞的兜底，不是预算")
    show_thinking = st.toggle("显示思考", value=True)
    allow_ask = st.toggle("允许问用户", value=True,
                          help="模型遇到含义不明确的业务名词时会停下来问你")
    st.divider()
    terms = _load_terms()
    st.caption(f"术语表：{len(terms)} 条（{TERMS_FILE.name}）")
    if terms and st.checkbox("看术语表"):
        st.json(terms)

deny = [c for c in deny_raw.split(",") if c.strip()]

# ── 主区 ──────────────────────────────────────────────────────────────────

question = st.text_input("问一句", placeholder="例：每个月分别有多少条记录？")
busy = st.session_state.get("run") is not None and not st.session_state["run"].done
if st.button("运行", type="primary", disabled=not (db and question) or busy):
    run = AgentRun(question, db, deny, int(max_steps), terms)
    st.session_state.run = run
    run.thread.start()
    st.rerun()

run: AgentRun | None = st.session_state.get("run")

if run is not None:
    st.caption(f"{run.db} · 问：{run.question}"
               + (f" · 禁查列：{', '.join(run.deny)}" if run.deny else ""))

    if not run.done:
        @st.fragment(run_every=0.5)
        def live() -> None:
            events, pending, done, error, _ = run.snapshot()
            if error:
                st.error(f"跑不起来：{error}")
                return
            if done:
                return
            if pending:
                st.warning(f"❓ **{pending.get('question', '')}**")
                cands = [str(c) for c in (pending.get("candidates") or []) if str(c).strip()]
                for i, c in enumerate(cands, 1):
                    if st.button(f"{i}. {c}", key=f"cand{i}"):
                        run.answer(c)
                        st.rerun()
                other = st.text_input("或者自己说", key="ask_other")
                if st.button("提交", key="ask_submit") and other.strip():
                    run.answer(other.strip())
                    st.rerun()
                return
            step = max((e.payload.get("step", 0) for e in events
                        if e.type == "step_start"), default=0)
            calls = sum(1 for e in events if e.type == "tool_call")
            st.status(f"跑着… 第 {step} 步 · 已调 {calls} 次工具", state="running")

        live()

    events, pending, done, error, result = run.snapshot()

    if done:
        if error:
            st.error(f"跑不起来：{error}")
        _render_timeline(events, show_thinking=show_thinking)
        if result:
            _render_result(result)
        if st.button("清空"):
            st.session_state.run = None
            st.rerun()
