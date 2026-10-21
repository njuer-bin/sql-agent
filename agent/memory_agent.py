"""W3 memory agent：记忆检索 → 口径对齐 → 复用 W2 纠错取数。

对应 PLAN.md W3「记忆检索」。与 error-fix 的差异（即 W3 消融变量）：
  1. memory 类问题不再拒答：检索记忆库 → 引用原文作答（含口径/情节/流程）
  2. 答案里引用的记忆条目作为工具输出留痕（数字可溯源，防幻觉误判）
  3. 需要计算的记忆题（如 E-004「算 9 月复购率」）按记忆口径取数（v2 剔除退款）
"""

from __future__ import annotations

import json

from agent.error_repair import RepairLoop
from agent.llm import LLMConfig
from agent.memory import DEFAULT_MEMORY_PATH, MemoryStore
from agent.nl2sql_offline import build_offline_sql
from agent.single_shot import ErrorFixAgent, SingleShotResult, _summarize_result
from agent.tools import ToolBox

_MEMORY_HEADER = "根据记忆库："


class MemoryAgent(ErrorFixAgent):
    """W3 被测 agent：混合检索记忆 + 口径对齐 + W2 纠错取数。"""

    name = "memory"

    def __init__(self, toolbox: ToolBox, llm_cfg: LLMConfig | None = None,
                 memory_path: str = DEFAULT_MEMORY_PATH, top_k: int = 6) -> None:
        super().__init__(toolbox, llm_cfg)
        self.name = MemoryAgent.name
        self.store = MemoryStore(memory_path)
        self.top_k = top_k

    def answer(self, question: dict) -> SingleShotResult:
        qtype = question.get("question_type", "data")
        text = str(question.get("question", ""))

        if qtype != "memory":
            return super().answer(question)

        out = SingleShotResult()
        entries = self.store.retrieve(text, top_k=self.top_k)
        out.retrieved = [e.get("memory_id") for e in entries]
        out.steps = 1  # 检索算一次工具调用（A4）

        if not entries:
            out.answer = "记忆库里没有能回答该问题的条目，我不会瞎猜。"
            out.error = "memory_miss"
            out.tool_outputs.append('{"note": "memory_retrieval_empty"}')
            return out

        # 记忆条目留痕：答案引用的口径/数字都能溯源
        dumped = self.store.dump(entries)
        out.tool_outputs.append(json.dumps({"memory": dumped}, ensure_ascii=False))

        parts = [f"【{d['title']}】{d['content']}" for d in dumped]
        out.answer = _MEMORY_HEADER + "；".join(parts) + "。"

        # 需要计算的记忆题（问题里有可解析的指标×时间）→ 按记忆口径取数
        sql = build_offline_sql(text)
        if sql is not None:
            loop = RepairLoop(self.tools, max_repairs=self.max_repairs)
            oc = loop.run(sql)
            out.steps += oc.steps
            for h in oc.history:
                if h.get("error_msg"):
                    out.tool_outputs.append(json.dumps(
                        {"repair_attempt": h["attempt"], "error_type": h["error_type"],
                         "error": h["error_msg"]}, ensure_ascii=False))
            if oc.ok:
                out.sql = oc.final_sql
                out.columns = oc.result["columns"]
                out.rows = oc.result["rows"]
                out.tool_outputs.append(
                    json.dumps(oc.result, ensure_ascii=False, default=str))
                out.answer += "按当前生效口径取数结果：" + _summarize_result(
                    text, out.columns, out.rows, oc.result["truncated"])
            else:
                out.answer += f"按当前生效口径取数失败：{oc.error}"
                out.tool_outputs.append(json.dumps({"error": oc.error},
                                                   ensure_ascii=False))
        return out
