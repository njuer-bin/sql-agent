"""v0.8 Supervisor 结构化调度：TaskPlan + 依赖关系 + 约束。

原来 Supervisor 只做规则路由（question_type → 专家名）。这里升级为
显式任务计划：

  问题 → intent 判定（含决策理由）→ TaskPlan（意图 / 专家集合 /
         专家间依赖 DAG / 阶段内 DAG / 约束：指标·地区·时间窗）→
         Executor 按拓扑序执行 → 每步产出 AgentMessage（见 protocol.py）

TaskPlan 可 JSON 序列化、可进 trace，最终答案可反查回「计划哪几步、
依赖谁、约束是什么」——计划层与执行层分开，是可审计调度的最小期望。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

INTENT_TO_EXPERT = {
    "data": "data",
    "memory": "retrieval",
    "attribution": "attribution",
}

# 专家间依赖：DAG 声明（当前版拓扑 = 单主专家；等 MCP/协作期扩起来）
EXPERT_DAG = {
    "data": {"data": []},           # 取数不依赖他人（schema 可自查）
    "retrieval": {"retrieval": []},  # 记忆检索独立
    "attribution": {"attribution": ["retrieval"]},
    # 归因语义上依赖检索结论（检索是它内部第一步），声明依赖供审计 /
}


@dataclass
class TaskPlan:
    """一次提问的显式执行计划（全字段 JSON 可序列化）。"""
    qid: str
    intent: str                                   # data / memory / attribution
    agents: list[str] = field(default_factory=list)
    graph: dict[str, list[str]] = field(default_factory=dict)   # 专家间依赖
    stage_dag: dict[str, list[str]] = field(default_factory=dict)  # 专家内阶段
    constraints: dict[str, Any] = field(default_factory=dict)  # 指标/地区/时间窗
    rationale: list[str] = field(default_factory=list)          # 决策理由

    def topological_order(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        pending = dict(self.graph)
        while pending:
            ready = [e for e, deps in pending.items()
                     if all(d in seen for d in deps)]
            if not ready:  # 环（防御；声明层不该出现）
                out.extend(sorted(pending))
                break
            for e in sorted(ready):
                out.append(e)
                seen.add(e)
                pending.pop(e)
        return out

    def to_json(self) -> str:
        return json.dumps({
            "qid": self.qid, "intent": self.intent,
            "agents": self.agents, "graph": self.graph,
            "stage_dag": self.stage_dag,
            "constraints": self.constraints,
            "rationale": self.rationale,
        }, ensure_ascii=False)


def build_stage_dag(expert: str) -> dict[str, list[str]]:
    """与 Supervisor.dag() 对齐的阶段 DAG（计划层的专家内部步骤）。"""
    if expert == "retrieval":
        return {"retrieval": ["memory_retrieve", "answer_compose", "evidence_trace"]}
    if expert == "attribution":
        return {"attribution": ["time_window", "sql_execute_current",
                                 "sql_execute_baseline", "memory_hypothesis",
                                 "drilldown", "verify_adversarial",
                                 "answer_compose"]}
    return {"data": ["sql_generate", "repair_loop", "answer_summarize"]}
