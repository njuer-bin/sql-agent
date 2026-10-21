"""single-shot adapter：把 agent 运行时接入评测框架（eval 的唯一边界）。

eval 框架不该 import agent 包，agent 实现细节全部封在这里面；
runner/verifier 只经由 ADAPTERS 注册表与 adapter.name 交互。
"""

from __future__ import annotations

import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.llm import LLMConfig  # noqa: E402
from agent.single_shot import SingleShotAgent  # noqa: E402
from agent.tools import ToolBox  # noqa: E402
from adapter_util import fill_cost  # noqa: E402
from verifier import AgentOutput  # noqa: E402

_AGENT: SingleShotAgent | None = None


def _get_agent() -> SingleShotAgent:
    global _AGENT
    if _AGENT is None:
        _AGENT = SingleShotAgent(ToolBox(), llm_cfg=LLMConfig())
    return _AGENT


class SingleShotAdapter:
    """W1 被测 agent：single-shot（单次 LLM 生成 / 无 Key 时离线 fallback）。"""

    name = "single-shot"

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
        fill_cost(out)
        return out
