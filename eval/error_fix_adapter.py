"""error-fix adapter：把 W2 纠错 agent 接入评测框架（边界模式同 single_shot_adapter）。"""

from __future__ import annotations

import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.error_repair import MAX_REPAIRS  # noqa: E402
from agent.llm import LLMConfig  # noqa: E402
from agent.single_shot import ErrorFixAgent  # noqa: E402
from agent.tools import ToolBox  # noqa: E402
from adapter_util import fill_cost  # noqa: E402
from verifier import AgentOutput  # noqa: E402

_AGENT: ErrorFixAgent | None = None


def _get_agent() -> ErrorFixAgent:
    global _AGENT
    if _AGENT is None:
        _AGENT = ErrorFixAgent(ToolBox(), llm_cfg=LLMConfig(), max_repairs=MAX_REPAIRS)
    return _AGENT


class ErrorFixAdapter:
    """W2 被测 agent：single-shot + SQL 自我纠错循环（最多 3 次修复重试）。"""

    name = "error-fix"

    def answer(self, question: dict, ctx: Any) -> AgentOutput:
        r = _get_agent().answer(question)

        out = AgentOutput()
        out.answer = r.answer
        out.sql = r.sql
        out.columns = r.columns
        out.result = r.rows
        out.steps = r.steps
        out.tool_outputs = list(r.tool_outputs or [])
        if r.error:
            out.execution_error = r.error
        out.repair_history = list(r.history or [])  # type: ignore[attr-defined]
        fill_cost(out)
        return out
