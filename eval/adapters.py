"""
Agent 适配器层（Adapters）
============================================================================
评测框架与被测 agent 之间的**唯一**边界。

为什么需要这一层：
  1. 评测框架不该知道被测 agent 内部怎么实现（可能是 LangGraph、可能是自研循环）
  2. 同一个评测集要跑多个配置做**消融对比**（Oracle / 无记忆 baseline / 完整 agent）
  3. 这样换 agent 实现时，46 道题和评分逻辑都不用动

约定：
  每个 adapter 实现一个方法
      answer(question: dict, ctx: RunContext) -> AgentOutput
  输入是题目 dict（就是 questions/*.jsonl 里的一行），
  输出是 AgentOutput（verifier.py 里定义的那个 dataclass）。

  ctx 提供只读工具（SQL 执行 / schema 查询 / 记忆检索），
  adapter 可以调用它们，也可以完全不调用（比如 baseline）。

内置三个 adapter：
  OracleAdapter      —— 直接返回 gold 答案，用于**验证评测框架本身没写错**
                        （它必须得 100 分，否则说明评分逻辑有 bug）
  NullAdapter        —— 什么都不答，用于**验证分数下限**（应该接近 0）
  RuleBaselineAdapter—— 一个不查记忆、不做多步推理的最简实现，
                        作为消融实验的对照组（"没有记忆模块"的那一组）
============================================================================
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Protocol

from verifier import AgentOutput

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, "data", "warehouse", "warehouse.db")


# --------------------------------------------------------------------------
# 运行上下文：给 adapter 的只读工具
# --------------------------------------------------------------------------

@dataclass
class RunContext:
    """
    一次评测运行的上下文。持有只读数据库连接和记忆库，
    所有 adapter 共享同一个 ctx（它们互不干扰，因为连接是只读的）。
    """
    db_path: str = DB
    memory_path: str = os.path.join(HERE, "memory_fixtures.jsonl")
    metrics_path: str = os.path.join(HERE, "metrics.yaml")
    max_rows: int = 1000
    _con: sqlite3.Connection | None = field(default=None, repr=False)
    _memory: list[dict] | None = field(default=None, repr=False)

    # ---- 工具 1：SQL 执行（只读） ----------------------------------------
    def sql(self, statement: str, params: tuple = ()) -> tuple[list[str], list[list[Any]]]:
        """执行只读 SQL，返回 (列名, 行)。非 SELECT 直接拒绝。"""
        head = " ".join(statement.strip().split())[:64].upper()
        if not head.startswith("SELECT") and not head.startswith("WITH"):
            raise ValueError(f"只允许只读查询，拒绝执行: {head}...")
        if self._con is None:
            self._con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        cur = self._con.execute(statement, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [list(r) for r in cur.fetchall()]
        if len(rows) > self.max_rows:
            rows = rows[: self.max_rows]
        return cols, rows

    # ---- 工具 2：记忆检索（关键词召回，供 baseline / 未来 agent 使用）----
    def search_memory(self, query: str, top_k: int = 5) -> list[dict]:
        """极简关键词检索：命中 keywords / title / content 的记忆条目。

        这只是**基线检索**，用来给"无记忆"和"有记忆"两组做对照。
        真正的 agent（带混合检索 + rerank + 时间衰减）在 agent/ 里实现。
        """
        if self._memory is None:
            self._memory = [
                json.loads(l) for l in open(self.memory_path, encoding="utf-8") if l.strip()
            ]
        q = query.lower()
        scored = []
        for m in self._memory:
            blob = " ".join(
                [m.get("title", "")] + m.get("keywords", []) + [m.get("content", "")]
            ).lower()
            hit = sum(1 for kw in m.get("keywords", []) if kw.lower() in q)
            if hit == 0 and q and q not in blob:
                continue
            scored.append((hit, m))
        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored[:top_k]]

    # ---- 工具 3：schema 查询 ------------------------------------------------
    def schema(self) -> str:
        """返回建表语句，供 agent 生成 SQL 时参考。"""
        p = os.path.join(ROOT, "data", "warehouse", "schema.sql")
        return open(p, encoding="utf-8").read()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None


# --------------------------------------------------------------------------
# 适配器协议
# --------------------------------------------------------------------------

class AgentAdapter(Protocol):
    """所有被测 agent 必须实现的接口。"""

    name: str

    def answer(self, question: dict, ctx: RunContext) -> AgentOutput:
        ...


# --------------------------------------------------------------------------
# 适配器 1：Oracle（gold 答案，用于自检评测框架）
# --------------------------------------------------------------------------

class OracleAdapter:
    """
    直接返回 gold 答案的"完美 agent"。

    它的用途**不是**参赛，而是**验证评测框架**：
      - 如果 Oracle 拿不到接近满分 → 说明 verifier 或题目本身有 bug
      - 它是所有其他 adapter 的分数天花板

    因此它必须：
      1. 返回 gold SQL + gold 结果（保证 execution/schema/result 满分）
      2. 答案文本覆盖所有 answer_contains / must_mention（保证 answer 满分）
      3. 证据覆盖 evidence_required（保证 evidence 满分）
      4. 检索命中所有期望记忆（保证 retrieval 满分）
    """

    name = "oracle"

    def answer(self, question: dict, ctx: RunContext) -> AgentOutput:
        exp = question.get("expected", {})
        qtype = question.get("question_type", "data")

        out = AgentOutput()
        out.sql = exp.get("reference_sql")
        out.columns = exp.get("columns")
        out.result = exp.get("result")
        out.steps = 1

        # 工具输出 = gold 结果本身，这样幻觉检测时数字都有出处
        if out.result is not None:
            out.tool_outputs = [
                json.dumps(
                    {"columns": out.columns, "rows": out.result},
                    ensure_ascii=False,
                )
            ]

        # 证据：覆盖 evidence_required（归因题）
        ev = list(exp.get("evidence_required") or [])
        if ev:
            out.evidence = ev

        # 记忆检索：命中所有期望条目（记忆题）
        out.retrieved_memory_ids = list(exp.get("memory_ids") or [])

        # 结论文本：把所有"必须出现"的关键词原样拼进去
        keys: list[str] = []
        keys += list(exp.get("answer_contains") or [])
        keys += list(exp.get("must_mention") or [])
        seen: set[str] = set()
        uniq = [k for k in keys if not (k in seen or seen.add(k))]

        parts = list(uniq)
        nums = _result_numbers(out.result, limit=24)
        if nums:
            parts.append("关键数值：" + "、".join(nums))
        out.answer = "；".join(parts) + "。" if parts else "（无结论）"

        return out


def _result_numbers(result: list[list[Any]] | None, limit: int = 24) -> list[str]:
    """把结果里的数字格式化成带千分位的字符串（与 gold 数值一致）。"""
    if not result:
        return []
    out: list[str] = []
    for row in result:
        for v in row:
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                out.append(f"{v:,}")
            elif isinstance(v, float):
                out.append(f"{v:,.2f}")
            if len(out) >= limit:
                return out
    return out


# --------------------------------------------------------------------------
# 适配器 2：Null（什么都不答，验证分数下限）
# --------------------------------------------------------------------------

class NullAdapter:
    """空回答。期望分数接近 0，用来确认评分器不会"白送分"。"""

    name = "null"

    def answer(self, question: dict, ctx: RunContext) -> AgentOutput:
        return AgentOutput(answer="", steps=0)


# --------------------------------------------------------------------------
# 适配器 3：RuleBaseline（不查记忆、单次生成，消融对照组）
# --------------------------------------------------------------------------

class RuleBaselineAdapter:
    """
    最简规则式实现 —— 消融实验里的 **"无记忆"** 对照组。

    它只做三件事：
      1. 看一眼 schema
      2. 用**固定模板**生成一条 SQL（不带任何口径约束）
      3. 把结果原样贴出来

    它故意**不做**的事（正是消融要验证的价值）：
      - 不查指标字典 → 会踩 cancelled 陷阱、会用错复购率口径
      - 不查历史记忆 → 归因题只能瞎猜
      - 不做多步推理 / 下钻 → 归因题给不出证据链

    注意：这是一个**确定性的最简 baseline**，不是 LLM。
    用它做对照的好处是：分数可复现、零成本、零 API 依赖。
    """

    name = "rule-baseline"

    # 问题里的时间/维度关键词 → SQL 片段（极简启发式）
    _MONTHS = {
        "1 月": ("01-01", "01-31"), "2 月": ("02-01", "02-28"), "3 月": ("03-01", "03-31"),
        "4 月": ("04-01", "04-30"), "5 月": ("05-01", "05-31"), "6 月": ("06-01", "06-30"),
        "7 月": ("07-01", "07-31"), "8 月": ("08-01", "08-31"), "9 月": ("09-01", "09-30"),
        "10 月": ("10-01", "10-31"), "11 月": ("11-01", "11-30"), "12 月": ("12-01", "12-31"),
    }

    def answer(self, question: dict, ctx: RunContext) -> AgentOutput:
        q = question.get("question", "")
        out = AgentOutput(steps=1)

        # 故意不读记忆、不读指标字典 —— 只看 schema
        out.tool_outputs = [ctx.schema()[:4000]]

        sql = self._guess_sql(q)
        out.sql = sql
        if sql is None:
            out.answer = "抱歉，我不确定该怎么取这个数。"
            return out

        try:
            cols, rows = ctx.sql(sql)
        except Exception as e:  # 执行失败 → 记录错误，不修复（baseline 没有纠错环）
            out.execution_error = str(e)
            out.answer = f"SQL 执行失败：{e}"
            return out

        out.columns, out.result = cols, rows
        out.tool_outputs.append(
            json.dumps({"columns": cols, "rows": rows}, ensure_ascii=False)
        )

        nums = _result_numbers(rows, limit=12)
        out.answer = "查询结果如下：" + ("、".join(nums) if nums else "（空）")
        return out

    # ---- 极简启发式 ------------------------------------------------------

    def _guess_sql(self, q: str) -> str | None:
        """从自然语言里猜一条 SQL。只覆盖最常见的两种问法。"""
        if "退款率" in q or "退货率" in q:
            where = self._time_clause(q)
            return (
                "SELECT ROUND(100.0*SUM(refund_amount)/SUM(pay_amount),2) AS refund_rate "
                "FROM fact_order " + (f"WHERE {where}" if where else "")
            )
        if "客单价" in q or "单均价" in q:
            where = self._time_clause(q)
            return (
                "SELECT ROUND(SUM(pay_amount)/COUNT(*),2) AS aov "
                "FROM fact_order " + (f"WHERE {where}" if where else "")
            )
        if "GMV" in q or "成交额" in q or "交易额" in q:
            where = self._time_clause(q)
            return (
                "SELECT ROUND(SUM(pay_amount),2) AS gmv "
                "FROM fact_order " + (f"WHERE {where}" if where else "")
            )
        if "订单" in q and ("多少" in q or "量" in q):
            where = self._time_clause(q)
            return (
                "SELECT COUNT(*) AS order_cnt FROM fact_order "
                + (f"WHERE {where}" if where else "")
            )
        # 其余一律不会 —— baseline 的能力边界就在这里
        return None

    def _time_clause(self, q: str) -> str | None:
        """从问题文本里抠出时间范围。**故意不加 order_status 过滤** —— 这是陷阱所在。"""
        year = "2026"
        for m, (s, e) in self._MONTHS.items():
            if m in q:
                return f"order_date BETWEEN '{year}-{s}' AND '{year}-{e}'"
        return None


# --------------------------------------------------------------------------
# 注册表：名字 → 类
# --------------------------------------------------------------------------

ADAPTERS: dict[str, type] = {
    "oracle": OracleAdapter,
    "null": NullAdapter,
    "rule-baseline": RuleBaselineAdapter,
    # W1 起从独立模块注册：agent 运行时细节不进本文件
    "single-shot": None,  # 用 try-import 兜底（下方）
}

try:  # noqa: SIM105  没装 agent 依赖时其余三组仍可正常评测
    from single_shot_adapter import SingleShotAdapter  # type: ignore

    ADAPTERS["single-shot"] = SingleShotAdapter
    from error_fix_adapter import ErrorFixAdapter  # type: ignore

    ADAPTERS["error-fix"] = ErrorFixAdapter
    from memory_adapter import MemoryAdapter  # type: ignore

    ADAPTERS["memory"] = MemoryAdapter
except ImportError:
    ADAPTERS.pop("single-shot", None)
    ADAPTERS.pop("error-fix", None)
    ADAPTERS.pop("memory", None)


def get_adapter(name: str) -> AgentAdapter:
    if name not in ADAPTERS:
        raise SystemExit(
            f"未知 adapter: {name}\n可选: {', '.join(sorted(ADAPTERS))}"
        )
    return ADAPTERS[name]()  # type: ignore[return-value]


if __name__ == "__main__":
    # 快速自检：Oracle 对单道题的输出
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = {
        "qid": "A-001",
        "question": "2026 年 9 月我们做了多少 GMV？",
        "question_type": "data",
        "expected": {
            "columns": ["gmv"],
            "result": [[12345.67]],
            "reference_sql": "SELECT ROUND(SUM(pay_amount),2) AS gmv FROM fact_order",
            "answer_contains": ["GMV"],
        },
    }
    ctx = RunContext()
    o = OracleAdapter().answer(q, ctx)
    print("sql     :", o.sql)
    print("columns :", o.columns)
    print("result  :", o.result)
    print("answer  :", o.answer)
    con.close()
