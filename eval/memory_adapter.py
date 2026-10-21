"""memory adapter：把 W3 记忆 agent 接入评测框架（边界模式同 single_shot_adapter）。"""

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
from agent.memory_agent import MemoryAgent  # noqa: E402
from agent.tools import ToolBox  # noqa: E402
from verifier import AgentOutput  # noqa: E402

_AGENT: MemoryAgent | None = None


def _get_agent() -> MemoryAgent:
    global _AGENT
    if _AGENT is None:
        _AGENT = MemoryAgent(ToolBox(), llm_cfg=LLMConfig(),
                             memory_path=DEFAULT_MEMORY_PATH)
    return _AGENT


class MemoryAdapter:
    """W3 被测 agent：混合检索记忆 + 口径对齐 + W2 纠错取数。"""

    name = "memory"

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
        if r.error:
            out.execution_error = r.error
        out.repair_history = list(r.history or [])  # type: ignore[attr-defined]
        return out
