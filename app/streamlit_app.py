"""W5 Streamlit 界面 + 报告生成。

对应 PLAN.md W5「提问 → SQL + 结果表 + 结论 + 置信度 + 一键复核」。

- build_report(question, AgentOutput) —— 与 UI 无关的报告生成（可测试）：
  结论 + SQL + 结果表 + 证据链 + 置信度估计
- streamlit UI 仅在 streamlit 安装时可用（import 放入函数内，缺库不炸）
- 一键复核：重跑 AdversarialVerifier（独立复核，不信 UI 内缓存）

运行：streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import json
import os
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
    lines = ["# 经营分析回答（可审计）", "", f"**问题**：{question.get('question', '')}", ""]
    lines.append(f"**结论**：{out.answer}")
    lines.append("")
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


def run_streamlit() -> None:  # pragma: no cover
    import streamlit as st  # 仅在安装了 streamlit 的环境执行

    st.set_page_config(page_title="经营分析多智能体助手", layout="wide")
    st.title("经营分析多智能体助手")
    st.caption("Supervisor + 取数/检索/归因专家 + 对抗式验证（offline / LLM 双通道）")

    toolbox = st.session_state.get("_toolbox") or ToolBox()
    st.session_state["_toolbox"] = toolbox
    supervisor = st.session_state.get("_supervisor")
    if supervisor is None:
        supervisor = Supervisor(toolbox, llm_cfg=LLMConfig(),
                                memory_path=DEFAULT_MEMORY_PATH)
        st.session_state["_supervisor"] = supervisor

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
        st.markdown(out.answer)
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
