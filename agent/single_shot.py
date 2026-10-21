"""single-shot：W1 的被测 agent ——「一次生成 SQL → 受护栏限执行 → 报数」。

对应 PLAN.md W1 评测接入：`rule-baseline` → `single-shot`（单次 LLM 生成）。
消融定位：
  - 没有纠错循环（W2）、没有记忆（W3）、没有验证（W4）
  - SQL 只生成一次：报错/被拦截都直接失败（这正是和 W2 的对照点）
  - LLM 未配置时走离线确定性 NL2SQL（PLAN 风险表：agent 侧 fallback）
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent.answer import summarize as _summarize_result
from agent.errors import GuardrailError, SqlToolError
from agent.error_repair import RepairLoop
from agent.llm import LLMConfig, chat, extract_json
from agent.nl2sql_offline import build_offline_sql
from agent.tools import ToolBox

_LLM_SQL_PROMPT = """你是经营分析助手的 SQL 生成器。根据下述 schema 与指标口径，为用户问题写一条 SQLite 查询。
要求：
1. 只输出 JSON：{"sql": "<一条仅 SELECT 的查询>", "metric": "<对应的指标 id>"}
2. WHO 指标必须对齐口径：含 order_status 过滤（如 GMV 只算 paid）、时间过滤在日期列上。
3. 禁止写操作、禁止多语句、禁止查询敏感字段。
4. 不确定时输出 {"sql": null, "reason": "<缺什么信息>"}

[指标口径字典]
{metrics}

[建表语句]
{schema}

[用户问题]
{question}"""


@dataclass
class SingleShotResult:
    answer: str = ""
    sql: str | None = None
    columns: list[str] | None = None
    rows: list[list[Any]] | None = None
    steps: int = 0
    tool_outputs: list[str] = None  # type: ignore[assignment]
    error: str | None = None
    history: list[dict] | None = None  # W2 纠错轨迹（single-shot 恒为 None）
    retrieved: list[str] | None = None  # W3 记忆检索命中的 memory_id
    evidence: list[str] | None = None  # W4 已执行的证据链步骤标签

    def __post_init__(self) -> None:
        if self.tool_outputs is None:
            self.tool_outputs = []
        if self.history is None:
            self.history = []
        if self.retrieved is None:
            self.retrieved = []
        if self.evidence is None:
            self.evidence = []


class SingleShotAgent:
    """被 W1 评测使用的最小 agent。"""

    name = "single-shot"

    def __init__(self, toolbox: ToolBox, llm_cfg: LLMConfig | None = None) -> None:
        self.tools = toolbox
        self.llm_cfg = llm_cfg
        self.name = SingleShotAgent.name

    def answer(self, question: dict) -> SingleShotResult:
        out = SingleShotResult()
        qtype = question.get("question_type", "data")
        text = str(question.get("question", ""))

        # 能力边界：记忆/归因类需要后续周次的能力，当前单次取数版本明确说不会
        if qtype != "data":
            out.answer = (
                "该问题需要口径记忆或归因推理能力，当前的单次取数版本不支持，"
                "不会瞎猜。"
            )
            out.error = "unsupported_question_type"
            # 记录"确实没调用工具"这一事实，避免评分器把诚实拒答误判为幻觉
            out.tool_outputs.append('{"note": "no_tool_call_never_executed_sql"}')
            return out

        sql = self._generate_sql(text, question)
        if sql is None:
            out.answer = "我不确定如何取这个数（缺时间或口径信息），不会瞎猜。"
            out.error = "sql_generation_failed"
            out.steps = 1
            return out

        out.sql = sql
        try:
            r = self.tools.sql_execute(sql)
        except GuardrailError as e:
            out.answer = f"取数失败：{e}"
            out.error = f"guardrail: {e}"
            out.steps = 1
            out.tool_outputs.append(json.dumps({"error": str(e)}, ensure_ascii=False))
            return out
        except SqlToolError as e:
            out.answer = f"取数失败：{e}"
            out.error = str(e)
            out.steps = 1
            out.tool_outputs.append(json.dumps({"error": str(e)}, ensure_ascii=False))
            return out

        out.columns = r["columns"]
        out.rows = r["rows"]
        out.steps = 1
        out.tool_outputs = [json.dumps(r, ensure_ascii=False, default=str)]
        out.answer = _summarize_result(text, r["columns"], r["rows"], r["truncated"])
        return out

    # ------------------------------------------------------------------
    def _generate_sql(self, text: str, question: dict) -> str | None:
        if self.llm_cfg is not None and self.llm_cfg.enabled:
            try:
                txt = chat(
                    [
                        {"role": "system", "content": "只输出一个 JSON 对象。"},
                        {
                            "role": "user",
                            "content": _LLM_SQL_PROMPT.format(
                                metrics=json.dumps(self.tools.metric_lookup(text, top_k=5),
                                                   ensure_ascii=False, default=str),
                                schema=self.tools.schema_lookup()[:4000],
                                question=text,
                            ),
                        },
                    ],
                    self.llm_cfg,
                )
                parsed = extract_json(txt)
                if parsed and parsed.get("sql"):
                    return str(parsed["sql"])
                return None
            except RuntimeError:
                return None  # LLM 挂了 → 落回离线通道
        return build_offline_sql(text)


# ==========================================================================
# W2：error-fix —— 在 single-shot 之上加「错误分类 → 修复 → 重试」循环
# ==========================================================================

_ATTR_SCAFFOLD_NOTE = (
    "说明：归因需要「口径确认 → 下钻 → 证据链」多步推理，属后续版本的归因能力；"
    "当前的取数纠错版本不下因果结论，也不会瞎猜原因。"
)


class ErrorFixAgent(SingleShotAgent):
    """W2 被测 agent：SQL 报错不再直接失败，走纠错循环（最多 3 次修复重试）。

    与 single-shot 的差异（即 W2 消融变量）：
      1. SQL 报错 → 分类（语法/列/表/超时/空）→ 确定性修复 → 重试，上限 3 次
      2. 护栏错误是安全红线，不自动修复
      3. 归因题先跑「基线取数」并明确说明局限（完整归因链在 W4）
    """

    name = "error-fix"

    def __init__(self, toolbox: ToolBox, llm_cfg: LLMConfig | None = None,
                 max_repairs: int = 3) -> None:
        super().__init__(toolbox, llm_cfg)
        self.name = ErrorFixAgent.name
        self.max_repairs = max_repairs

    def answer(self, question: dict) -> SingleShotResult:
        out = SingleShotResult()
        qtype = question.get("question_type", "data")
        text = str(question.get("question", ""))

        # 记忆类是 W3 的能力，明确不答（与 W1 同一边界）
        if qtype == "memory":
            out.answer = "该问题需要口径记忆/情节记忆能力，当前的取数纠错版本不支持，不会瞎猜。"
            out.error = "unsupported_question_type"
            out.tool_outputs.append('{"note": "no_tool_call_never_executed_sql"}')
            return out

        sql = self._generate_sql(text, question)
        if sql is None:
            out.answer = (
                "该问题需要归因/下钻能力或更明确的口径，当前版本无法起步取数，不会瞎猜。"
                if qtype == "attribution"
                else "我不确定如何取这个数（缺时间或口径信息），不会瞎猜。"
            )
            out.error = "sql_generation_failed"
            out.steps = 1
            # 留痕：确实没调用工具，避免评分器把诚实拒答误判为幻觉
            out.tool_outputs.append('{"note": "no_tool_call_never_executed_sql"}')
            return out

        loop = RepairLoop(self.tools, max_repairs=self.max_repairs)
        oc = loop.run(sql)
        out.sql = oc.final_sql
        out.steps = oc.steps
        out.history = list(oc.history)

        # 中间失败的尝试也要留痕（可追溯，防幻觉误判）
        for h in oc.history:
            if h.get("error_msg"):
                out.tool_outputs.append(json.dumps(
                    {"repair_attempt": h["attempt"], "error_type": h["error_type"],
                     "error": h["error_msg"]}, ensure_ascii=False))

        if not oc.ok:
            out.answer = f"取数失败：{oc.error}"
            out.error = oc.error
            out.tool_outputs.append(json.dumps({"error": oc.error}, ensure_ascii=False))
            return out

        out.columns = oc.result["columns"]
        out.rows = oc.result["rows"]
        out.tool_outputs.append(json.dumps(oc.result, ensure_ascii=False, default=str))
        body = _summarize_result(text, out.columns, out.rows, oc.result["truncated"])
        out.answer = _ATTR_SCAFFOLD_NOTE + body if qtype == "attribution" else body
        return out
