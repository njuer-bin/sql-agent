"""multi-agent adapter：把 W4 Supervisor 编排器接入评测框架。"""

from __future__ import annotations

import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.llm import LLMConfig  # noqa: E402
from agent.memory import DEFAULT_MEMORY_PATH  # noqa: E402
from agent.multi_agent import Supervisor  # noqa: E402
from agent.tools import ToolBox  # noqa: E402
from adapter_util import fill_cost  # noqa: E402
from verifier import AgentOutput  # noqa: E402

_AGENT: Supervisor | None = None


def _get_agent() -> Supervisor:
    global _AGENT
    if _AGENT is None:
        _AGENT = Supervisor(ToolBox(), llm_cfg=LLMConfig(),
                            memory_path=DEFAULT_MEMORY_PATH)
    return _AGENT


class MultiAgentAdapter:
    """W4 被测 agent：Supervisor 路由 + 私有记忆隔离 + 对抗式验证。"""

    name = "multi-agent"

    def answer(self, question: dict, ctx: Any) -> AgentOutput:
        r = _get_agent().answer(question)

        out = AgentOutput()
        out.answer = r.answer
        out.sql = r.sql
        out.columns = r.columns
        out.result = r.rows
        out.steps = r.steps
        out.tool_outputs = list(r.tool_outputs or [])
        out.retrieved_memory_ids = list(r.retrieved or [])
        out.evidence = list(r.evidence or [])
        if r.error:
            out.execution_error = r.error
        out.repair_history = list(r.history or [])  # type: ignore[attr-defined]
        fill_cost(out)
        return out
