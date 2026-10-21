"""W5 Streamlit 界面 + 报告生成。

对应 PLAN.md W5「提问 → SQL + 结果表 + 结论 + 置信度 + 一键复核」。

- build_report(question, AgentOutput) —— 与 UI 无关的报告生成（可测试）：
  结论 + SQL + 结果表 + 证据链 + 置信度估计
- streamlit UI 仅在 streamlit 安装时可用（import 放入函数内，缺库不炸）
- 一键复核：重跑 AdversarialVerifier（独立复核，不信 UI 内缓存）

运行：streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import glob
import io
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.tools import ToolBox  # noqa: E402
from agent.verifier_adv import AdversarialVerifier  # noqa: E402
from agent.multi_agent import Supervisor  # noqa: E402
from agent.memory import DEFAULT_MEMORY_PATH  # noqa: E402
from agent.llm import LLMConfig  # noqa: E402

# 结果里最长的表预览
_TABLE_MAX_ROWS = 8

# 用户上传数据落盘位置 + 合法库名
UPLOAD_DIR = os.path.join(ROOT, "data", "uploaded")
_SAFE_SQL_NAME = re.compile(r"^[A-Za-z0-9_\-]+$")


def _handle_uploads(files, target_db: str, msgs: list[str]) -> None:
    """把上传的 CSV / SQL dump 写入 target_db（SQLite）。

    - *.csv：pandas 读取（utf-8-sig → gbk → utf-8 编码回退）→ 一张 table
    - 其他（.sql/.txt/.dump）：按 SQLite 脚本整体执行（本项目自产 dump 兼容）
    单个文件失败不影响其余文件，错误收集进 msgs。
    """
    import pandas as pd

    os.makedirs(os.path.dirname(target_db), exist_ok=True)
    con = sqlite3.connect(target_db)
    try:
        for f in files:
            try:
                raw = f.getvalue()
                if f.name.lower().endswith(".csv"):
                    df = None
                    last_err: Exception | None = None
                    for enc in ("utf-8-sig", "gbk", "utf-8"):
                        try:
                            df = pd.read_csv(io.BytesIO(raw), encoding=enc)
                            break
                        except (ValueError, UnicodeDecodeError) as e:
                            last_err = e
                    if df is None:
                        msgs.append(("err", f"{f.name}: 解析失败 {last_err}"))
                        continue
                    tname = os.path.splitext(os.path.basename(f.name))[0]
                    tname = re.sub(r"[^\w\-]+", "_", tname).strip("_") or "table"
                    df.to_sql(tname, con, if_exists="replace", index=False)
                    msgs.append(("ok",
                                 f"{f.name} → 表 {tname}（{len(df)} 行 × "
                                 f"{len(df.columns)} 列）"))
                else:
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        text = raw.decode("gbk", "replace")
                    con.executescript(text)
                    msgs.append(("ok", f"{f.name}: SQL 脚本已执行"))
            except sqlite3.Error as e:
                msgs.append(("err", f"{f.name}: SQL 执行失败 {e}"))
        con.commit()
    finally:
        con.close()


def _render_upload_section() -> None:  # pragma: no cover
    """侧栏上传区：CSV/SQL → SQLite（新库或追加到当前库）。"""
    import streamlit as st

    files = st.file_uploader(
        "上传 CSV / SQL 数据（可多选）",
        type=["csv", "sql", "txt", "dump"],
        accept_multiple_files=True,
        key="_datasrc_upload",
    )
    files = files or []
    sig = ",".join(f"{f.name}:{f.size}" for f in files)
    if files and st.session_state.get("_upload_sig") != sig:
        mode = st.radio("写入目标", ["新建库", "追加到当前库"],
                        horizontal=True, key="_up_mode")
        target = None
        if mode == "新建库":
            name = st.text_input("新库名（字母/数字/-/_）", key="_up_name",
                                 placeholder="uploaded_sales")
            if name and _SAFE_SQL_NAME.match(name.strip()):
                target = os.path.join(UPLOAD_DIR, name.strip() + ".db")
            elif name:
                st.caption("库名只允许字母、数字、-、_")
        else:
            prev = st.session_state.get("_db_sel")
            if prev and os.path.exists(prev):
                target = prev
            else:
                st.caption("当前没有已选中的数据库，请先选择或改为新建库")
        if st.button("导入", type="primary", key="_up_go",
                     disabled=not target):
            msgs: list[tuple[str, str]] = []
            _handle_uploads(files, target, msgs)
            st.session_state["_upload_sig"] = sig
            st.session_state["_db_sel"] = target  # selectbox 尚未实例化
            for kind, m in msgs:
                (st.success if kind == "ok" else st.error)(m)
            st.rerun()


def _result_table(agent_output) -> list[list] | None:
    rows = getattr(agent_output, "result", None)
    cols = getattr(agent_output, "columns", None) or []
    if rows and cols:
        return [cols] + [list(r) for r in rows[:_TABLE_MAX_ROWS]]
    return None


def _confidence(agent_output, question: dict) -> float:
    """置信度估计：事件分量线性合成（0~1）。"""
    s = 0.0
    if getattr(agent_output, "sql", None):
        has_rows = bool(getattr(agent_output, "result", None))
        s += 0.5 if has_rows else 0.2
        if not (getattr(agent_output, "execution_error", None)):
            s += 0.2
    ev = getattr(agent_output, "evidence", None) or []
    s += min(0.2, 0.04 * len(ev))
    mem = getattr(agent_output, "retrieved_memory_ids", None) or []
    exp_mem = (question.get("expected") or {}).get("memory_ids") or []
    if exp_mem:
        s += 0.1 * len(set(exp_mem) & set(mem)) / len(exp_mem)
    return round(min(1.0, s), 2)


def build_report(question: dict, out) -> str:
    """结论 + 证据链 + SQL + 结果表 + 置信度的可审计报告（Markdown）。"""
    lines: list[str] = ["# 经营分析回答（可审计）", "",
                        f"**问题**：{question.get('question', '')}", "",
                        # Streamlit/MD 会把「01~2026」当上下标语法吞字符，转义后原样展示
                        "**结论**：" + str(out.answer).replace("~", "\\~"), ""]
    ev = getattr(out, "evidence", None) or []
    if ev:
        lines.append("**证据链步骤**：" + " → ".join(ev))
        lines.append("")
    sql = getattr(out, "sql", None)
    if sql:
        lines += ["**SQL**：", "```sql", sql, "```", ""]
    tbl = _result_table(out)
    if tbl:
        lines.append("**结果表（前 %d 行）**：" % (len(tbl) - 1))
        header = "| " + " | ".join(str(c) for c in tbl[0]) + " |"
        sep = "| " + " | ".join(["---"] * len(tbl[0])) + " |"
        body = "\n".join("| " + " | ".join(str(c) for c in r) + " |"
                         for r in tbl[1:])
        lines += [header, sep, body, ""]
    trs = [t for t in (getattr(out, "tool_outputs", None) or []) if '"verifier"' in t]
    if trs:
        try:
            v = json.loads(trs[-1])["verifier"]
            state = "通过" if v["passed"] else "拦截: " + "; ".join(v["issues"])
        except (ValueError, KeyError):
            state = "不可读"
        lines.append(f"**对抗验证**：{state}")
        lines.append("")
    lines.append(f"**置信度**：{_confidence(out, question)}")
    return "\n".join(lines)


def recheck(out) -> dict:
    """一键复核：独立重跑对抗 Verifier（不复用结果里的缓存裁定）。"""
    v = AdversarialVerifier().check(out.answer, out.tool_outputs or [])
    return {"passed": v.passed,
            "issues": [f"[{i.check}] {i.detail}" for i in v.issues]}


def _browse_table(db_path: str) -> None:  # pragma: no cover
    """侧栏数据浏览：选表 → 预览样例行（只读）。"""
    import streamlit as st  # 仅在安装了 streamlit 的环境执行
    if not os.path.exists(db_path):
        st.sidebar.warning("文件不存在，取数会失败")
        return
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        try:
            tb = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()]
        finally:
            con.close()
    except sqlite3.Error as e:
        st.sidebar.warning(f"无法读取：{e}")
        return
    if not tb:
        st.sidebar.caption("(空库)")
        return
    st.session_state.setdefault("_table", tb[0])
    sel = st.sidebar.selectbox(
        "浏览数据表", tb,
        index=tb.index(st.session_state["_table"]) if st.session_state["_table"] in tb else 0,
        key="_table_sel",
    )
    st.session_state["_table"] = sel
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        try:
            (n,) = con.execute(f'SELECT COUNT(*) FROM "{sel}"').fetchone()
            cols = [d[1] for d in con.execute(f'PRAGMA table_info("{sel}")').fetchall()]
            rows = con.execute(f'SELECT * FROM "{sel}" LIMIT 20').fetchall()
        finally:
            con.close()
        st.sidebar.caption(f"{sel}：{n} 行")
        with st.sidebar.expander("样例行（前 20）", expanded=False):
            st.dataframe([cols] + rows, use_container_width=True)
    except sqlite3.Error as e:
        st.sidebar.caption(f"读取 {sel} 失败：{e}")


def _list_dbs(root: str) -> list[str]:
    """仓库内全部候选数据文件（data/ 递归，*.db）。"""
    return sorted(
        glob.glob(os.path.join(root, "data", "**", "*.db"), recursive=True)
    )


def run_streamlit() -> None:  # pragma: no cover
    import streamlit as st  # 仅在安装了 streamlit 的环境执行

    st.set_page_config(page_title="经营分析多智能体助手", layout="wide")
    st.title("经营分析多智能体助手")
    st.caption("Supervisor + 取数/检索/归因专家 + 对抗式验证（offline / LLM 双通道）")

    # 侧栏：上传数据（CSV/SQL → sqlite）+ 选择数据文件
    with st.sidebar:
        st.header("数据源")
        _render_upload_section()
        dbs = _list_dbs(ROOT)
        prev = st.session_state.get("_db_sel")
        if prev and prev not in dbs:
            st.session_state.pop("_db_sel", None)  # 旧库被删时重置选择
        db_choice = st.selectbox(
            "数据库文件", dbs if dbs else ["(data/ 下没有 .db 文件)"],
            format_func=lambda p: os.path.relpath(p, ROOT),
            key="_db_sel",
        )
        if db_choice.startswith("("):
            db_choice = ""
        if db_choice:
            _browse_table(db_choice)

    toolbox = st.session_state.get("_toolbox")
    if toolbox is None or (db_choice and toolbox.db_path != db_choice):
        toolbox = ToolBox(db_path=db_choice or ToolBox().db_path)
        st.session_state["_toolbox"] = toolbox
        supervisor = Supervisor(toolbox, llm_cfg=LLMConfig(),
                                memory_path=DEFAULT_MEMORY_PATH)
        st.session_state["_supervisor"] = supervisor
    supervisor = st.session_state["_supervisor"]

    default_q = st.session_state.get("demo_question")
    question = st.text_input("问题", value=default_q or "",
                             placeholder="例：2026 年 9 月华北的退款率为什么突然升高了？")
    if st.button("回答", type="primary") and question.strip():
        with st.spinner("Supervisor 路由 → 专家 → 对抗验证 ..."):
            out_raw = supervisor.answer(
                {"qid": "ui-" + str(hash(question) % 10000), "question": question})
            out = type("O", (), {})()
            out.answer = out_raw.answer
            out.sql = out_raw.sql
            out.columns = out_raw.columns
            out.result = out_raw.rows
            out.evidence = out_raw.evidence
            out.tool_outputs = out_raw.tool_outputs
            out.retrieved_memory_ids = out_raw.retrieved
            out.execution_error = out_raw.error
            out.steps = out_raw.steps

        st.subheader("结论")
        # Streamlit 的 Markdown 把「01~2026」当上/下标语法吞字符，转义后原样展示
        st.markdown(out.answer.replace("~", "\\~"))
        c1, c2, c3 = st.columns(3)
        c1.metric("置信度", _confidence(out, {"question": question}))
        c2.metric("步数", out.steps)
        c3.metric("记忆引用", len(out.retrieved_memory_ids or []))
        if out.sql:
            with st.expander("SQL", expanded=False):
                st.code(out.sql, language="sql")
        tbl = _result_table(out)
        if tbl:
            with st.expander("结果表", expanded=False):
                st.dataframe(tbl[1:], use_container_width=True)
                st.caption("列：" + "、".join(str(c) for c in tbl[0]))
        if out.evidence:
            st.markdown("**证据链**：" + " → ".join(out.evidence))
        if st.button("一键复核"):
            st.json(recheck(out))
        with st.expander("全链路工具输出（可审计）", expanded=False):
            for to in out.tool_outputs:
                st.text(to[:400])


if __name__ == "__main__":
    run_streamlit()
