"""工具层：sql_execute / schema_lookup / metric_lookup。

对应 PLAN.md W1「工具层」：
  - sql_execute：只读连接 + 10s 超时 + 1000 行上限 + guardrails 拦截
  - schema_lookup：返回建表语句（可只筛选语句中涉及的表）
  - metric_lookup：从 metrics.yaml 查指标口径（名称/别名命中）

工具层是唯一执行 SQL 的入口，eval 的 RunContext 之外的所有下游
（ReAct agent、后续 multi-agent）都必须经由它取数。
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from typing import Any

import yaml

from agent.errors import GuardrailError, SqlToolError
from agent.guardrails import FACT_TABLES, validate_sql

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "warehouse", "warehouse.db")
SCHEMA_PATH = os.path.join(ROOT, "data", "warehouse", "schema.sql")
METRICS_PATH = os.path.join(ROOT, "eval", "metrics.yaml")

DEFAULT_TIMEOUT_SEC = 10.0
DEFAULT_MAX_ROWS = 1000


class ToolBox:
    """无状态工具集合。持有自己的只读连接；eval 的 ctx 与 ToolBox 相互独立。"""

    def __init__(
        self,
        db_path: str = DB_PATH,
        schema_path: str = SCHEMA_PATH,
        metrics_path: str = METRICS_PATH,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        self.db_path = db_path
        self.schema_path = schema_path
        self.metrics_path = metrics_path
        self.timeout_sec = timeout_sec
        self.max_rows = max_rows
        self._con: sqlite3.Connection | None = None
        self._t_start: float = 0.0

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    # ------------------------------------------------------------------
    # 工具 1：sql_execute
    # ------------------------------------------------------------------
    def sql_execute(self, statement: str, params: tuple = ()) -> dict[str, Any]:
        """执行只读 SQL。返回 {columns, rows, rowcount, truncated, elapsed_ms}。

        任何违规/失败都以异常形式上抛，绝不静默吞掉。
        """
        # 护栏先行（即使连接还没建）
        statement = validate_sql(statement)

        if self._con is None:
            con = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, timeout=self.timeout_sec
            )
            # 进度回调：每 1024 条 VM 指令检查一次墙钟，到点中断取数
            con.set_progress_handler(self._timeout_tick, 1024)
            self._con = con

        self._t_start = time.monotonic()
        try:
            cur = self._con.execute(statement, params)
            cols: list[str] = []
            rows: list[list[Any]] = []
            truncated = False
            if cur.description:
                cols = [d[0] for d in cur.description]
                # 行数硬上限：多取 1 行用于判定 truncated
                while True:
                    batch = cur.fetchmany(2048)
                    if not batch:
                        break
                    for r in batch:
                        if len(rows) >= self.max_rows:
                            truncated = True
                            break
                        rows.append(list(r))
                    if truncated:
                        break
            elapsed_ms = int((time.monotonic() - self._t_start) * 1000)
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "interrupted" in msg:
                raise SqlToolError(
                    f"SQL 执行超时（>{self.timeout_sec:.0f}s），已中断: "
                    f"{_preview(statement)}" ) from e
            raise SqlToolError(f"SQL 执行失败: {msg}") from e
        except sqlite3.Warning as e:
            raise SqlToolError(f"SQL 执行警告: {e}") from e

        return {
            "columns": cols,
            "rows": rows,
            "rowcount": len(rows),
            "truncated": truncated,
            "elapsed_ms": elapsed_ms,
        }

    def _timeout_tick(self) -> int:
        """sqlite progress handler：返回非 0 → 中断查询。"""
        if time.monotonic() - self._t_start > self.timeout_sec:
            return 1
        return 0

    # ------------------------------------------------------------------
    # 工具 2：schema_lookup
    # ------------------------------------------------------------------
    _SCHEMA_SNIPPET = re.compile(
        r"-- [^\n]*\n|^CREATE TABLE\s+(\w+).*?;.*?(?=\n\n|\Z)",
        re.DOTALL | re.MULTILINE,
    )

    def schema_lookup(self, keywords: list[str] | None = None) -> str:
        """返回建表语句。给定 keywords（表名/列名）时只返回相关表。"""
        text = open(self.schema_path, encoding="utf-8").read()
        blocks = re.split(r"\n\n(?=CREATE TABLE|-- \w)", text)
        if not keywords:
            return text
        kws = [k.lower() for k in keywords if k]
        hits = [b for b in blocks if any(k in b.lower() for k in kws)]
        return "\n\n".join(hits) if hits else text[:4000]

    # ------------------------------------------------------------------
    # 工具 3：metric_lookup
    # ------------------------------------------------------------------
    def metric_lookup(self, query: str, top_k: int = 3) -> list[dict]:
        """按名称/别名/关键词查指标口径，返回最相关的指标定义列表。"""
        doc = yaml.safe_load(open(self.metrics_path, encoding="utf-8"))
        metrics: list[dict] = doc.get("metrics", [])
        q = query.lower()
        scored: list[tuple[int, float, dict]] = []
        for m in metrics:
            score = 0.0
            mid = str(m.get("metric_id", "")).lower()
            name = str(m.get("name", ""))
            if mid in q:
                score += 3.0
            if name and name in q:
                score += 2.0
            for alias in m.get("aliases", []) or []:
                if str(alias).lower() in q:
                    score += 1.5
            for kw in m.get("keywords", []) or []:
                if str(kw).lower() in q:
                    score += 1.0
            if score > 0:
                scored.append((1, score, m))
        scored.sort(key=lambda x: -x[1])
        return [m for _, _, m in scored[:top_k]]

    # ------------------------------------------------------------------
    # 兼容入口：让旧评测链路（RunContext 风格）拿到同样的工具行为
    # ------------------------------------------------------------------
    def sql(self, statement: str, params: tuple = ()) -> tuple[list[str], list[list[Any]]]:
        """与 eval.RunContext.sql 相同签名，内部走 sql_execute。"""
        r = self.sql_execute(statement, params)
        return r["columns"], r["rows"]


def _preview(sql: str, limit: int = 80) -> str:
    return " ".join(sql.split())[:limit]


def fact_date_col(table: str) -> str | None:
    return FACT_TABLES.get(table)
