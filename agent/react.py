"""ReAct 单 agent 循环（W1）。

- 工具集：sql_execute / schema_lookup / metric_lookup（均经 ToolBox，护栏生效）
- 步数预算：最多 max_steps（默认 3）次工具调用；超限熔断，
  返回**明确的失败原因**而不是截断的思考过程（为 W2 的失败语义打底）
- LLM 已配置 → 行动由 chat completion 的 JSON 决定；未配置 → 确定性
  OfflinePlanner 兜底（零 API 依赖）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.answer import summarize
from agent.llm import LLMConfig, chat, extract_json
from agent.nl2sql_offline import build_offline_sql
from agent.tools import ToolBox

SYSTEM_PROMPT = """你是一个经营分析助手，只能通过工具取数，禁止臆造数字。
可用的工具（以 JSON 响应行动）：
  {"action": "sql_execute",  "args": {"statement": "<仅 SELECT>"}}
  {"action": "schema_lookup", "args": {"keywords": ["<表名或列名>"]}}
  {"action": "metric_lookup", "args": {"query": "<指标名>"}}
或者给出最终结论：
  {"final": true, "answer": "<给用户的中文结论，引用工具返回的数字>"}

要求：
1. 需要 SQL 时必须先对齐 metric_lookup 里的口径（时间过滤、order_status 等）。
2. 每次只调用一个工具，等待观察结果。
3. 最终答案里的每个数字都必须来自工具输出。"""


@dataclass
class AgentResult:
    answer: str = ""
    sql: str | None = None
    columns: list[str] | None = None
    rows: list[list[Any]] | None = None
    steps: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    tool_outputs: list[str] = field(default_factory=list)
    error: str | None = None
    truncated: bool = False


class ReActAgent:
    """通用 ReAct 单 agent。LLM 未配置时退化为离线确定性计划。"""

    def __init__(
        self,
        toolbox: ToolBox,
        llm_cfg: LLMConfig | None = None,
        max_steps: int = 3,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self.tools = toolbox
        self.llm_cfg = llm_cfg
        self.max_steps = max_steps
        self.system_prompt = system_prompt

    # ------------------------------------------------------------------
    def run(self, question: str) -> AgentResult:
        result = AgentResult()
        history: list[dict] = []  # [{"action":..., "observation":...}]

        while result.steps < self.max_steps:
            act = self._next_action(question, history)
            if act.get("final"):
                result.answer = str(act.get("answer", "")) or result.answer
                return result

            action = str(act.get("action", "")).strip()
            if action not in ("sql_execute", "schema_lookup", "metric_lookup"):
                # 不认识的行动 → 视为该步失败但不烧断预算
                history.append({"action": "unknown", "observation": f"未知 action: {action}"})
                result.steps += 1
                continue

            try:
                obs = self._dispatch(action, act.get("args", {}), result)
            except Exception as e:  # noqa: BLE001  工具异常转为观察，不中断整局
                obs = {"error": f"{type(e).__name__}: {e}"}
                result.tool_calls.append({"action": action, "args": act.get("args", {}), "failed": True})
                history.append({"action": action, "observation": obs})
                result.steps += 1
                # 离线模式没有 LLM 来重试，直接熔断给原因
                if not self._llm_ready():
                    result.answer = f"取数失败：工具调用出错：{obs['error']}"
                    result.error = obs['error']
                    result.tool_outputs.append(
                        json.dumps({"error": obs["error"]}, ensure_ascii=False)
                    )
                    return result
                continue

            result.tool_calls.append({"action": action, "args": act.get("args", {})})
            history.append({"action": action, "observation": obs})
            result.steps += 1

            if action == "sql_execute":
                # LLM 模式：让模型基于观察生成结论文本
                if self._llm_ready():
                    final = self._next_action(
                        question,
                        history
                        + [{
                            "action": "conclude",
                            "observation": {"answer_hint": "请基于以上工具输出生成最终结论"},
                        }],
                    )
                    result.answer = str(final.get("answer", "")) or summarize(question, result.columns, result.rows, result.truncated)
                else:
                    result.answer = summarize(question, result.columns, result.rows, result.truncated)
                return result

        result.answer = (
            "取数失败：已达本局工具调用上限，该问题在当前配置下无法完成。"
        )
        result.error = "max_tool_calls_exceeded"
        result.tool_outputs.append(json.dumps({"error": result.error}, ensure_ascii=False))
        return result

    # ------------------------------------------------------------------
    def _dispatch(self, action: str, args: dict, result: AgentResult) -> dict:
        if action == "sql_execute":
            stmt = str(args.get("statement", args.get("sql", "")))
            r = self.tools.sql_execute(stmt)
            result.sql = stmt
            result.columns = r["columns"]
            result.rows = r["rows"]
            result.truncated = r["truncated"]
            result.tool_outputs.append(json.dumps(r, ensure_ascii=False, default=str))
            return r

        if action == "schema_lookup":
            text = self.tools.schema_lookup(args.get("keywords"))
            result.tool_outputs.append(text[:2000])
            return {"schema": text[:2000]}

        if action == "metric_lookup":
            metrics = self.tools.metric_lookup(str(args.get("query", "")))
            result.tool_outputs.append(json.dumps(metrics, ensure_ascii=False, default=str))
            return {"metrics": metrics}

        raise ValueError(f"未知 action: {action}")

    # ------------------------------------------------------------------
    def _llm_ready(self) -> bool:
        return (self.llm_cfg is not None and self.llm_cfg.enabled)

    def _next_action(self, question: str, history: list[dict]) -> dict:
        """LLM 可用 → 调 chat（JSON 容错解析）；否则 OfflinePlanner。"""
        if self._llm_ready():
            msgs: list[dict] = [{"role": "system", "content": self.system_prompt},
                                {"role": "user", "content": question}]
            for h in history:
                msgs.append({"role": "user", "content": f"上一步 {h['action']} 的观察："})
                msgs.append({"role": "user", "content": json.dumps(h["observation"], ensure_ascii=False)[:1200]})
            cfg = self.llm_cfg
            try:
                text = chat(msgs, cfg)
            except RuntimeError as e:
                return {"final": True, "answer": f"取数失败：LLM 调用异常：{e}"}
            parsed = extract_json(text)
            if parsed is None:
                return {"final": True, "answer": f"取数失败：LLM 输出无法解析为 JSON：{text[:120]}"}
            if "final" in parsed or "answer" in parsed:
                return {"final": True, "answer": parsed.get("answer", "")}
            return parsed

        return self._offline_plan(question, history)

    # ------------------------------------------------------------------
    def _offline_plan(self, question: str, history: list[dict]) -> dict:
        """确定性计划：探指标口径 → (可选) 探 schema → sql_execute → 终答。"""
        did_lookup = any(h["action"] == "metric_lookup" for h in history)
        if not did_lookup:
            return {"action": "metric_lookup", "args": {"query": question}}

        sql = build_offline_sql(question)
        if sql is None:
            return {
                "final": True,
                "answer": "我不确定如何取这个数（超出单次生成的覆盖范围），不会瞎猜。",
            }
        return {"action": "sql_execute", "args": {"statement": sql}}
