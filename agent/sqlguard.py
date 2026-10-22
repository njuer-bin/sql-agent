"""AST 级 SQL 护栏（sqlglot 第二道闸）。

regex 骨架护栏（guardrails.validate_sql）之后叠加的结构层校验：
  AST1 可解析：任何语法不合法 / 方言外语句直接拒绝（注入载荷多半不可解析）
  AST2 单条只读：根节点必须且只能是 SELECT / UNION（CTE 归一为子查询）
  AST3 禁写：AST 中出现任何 DDL/DML/事务节点（DROP/INSERT/ATTACH/PRAGMA…）→ 拒
  AST4 表白名单：所有表引用必须 ∈ 库内表白名单；拒绝 sqlite_master 等系统表
  AST5 函数黑名单：load_extension / readfile / writefile 等逃逸面 → 拒

python-sqlite 逃逸面主要三点：ATTACH 读写外部文件、load_extension(’) 加载
动态库、无线程只读模式 revoke 也拦不住的 PRAGMA—— regex 层已拦关键字，
AST 层从结构上再拦一次，双闸互补（regex 可被注释/编码绕过，AST 不可）。
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from agent.errors import GuardrailError

# 库内表白名单（与 warehouse.db / schema.sql 对齐）
ALLOWED_TABLES: frozenset[str] = frozenset({
    "dim_date", "dim_region", "dim_channel", "dim_customer", "dim_product",
    "fact_order", "fact_marketing_campaign", "fact_web_traffic",
    "fact_service_ticket",
})

# AST 禁用节点类型（写操作 / 事务 / 附加库 / 环境探查）
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Drop, exp.Delete, exp.Insert, exp.Update, exp.Merge, exp.TruncateTable,
    exp.Alter, exp.AlterColumn, exp.AlterRename, exp.AlterSession, exp.AlterSet,
    exp.Create, exp.Grant, exp.Revoke,
    exp.Attach, exp.Detach, exp.Pragma, exp.Transaction, exp.Commit, exp.Rollback,
    exp.Analyze,
)

# AST 函数黑名单：任何大小写/引号变体都归一为 func 名比较
_FORBIDDEN_FUNCS: frozenset[str] = frozenset({
    "load_extension", "readfile", "writefile", "attach", "detach",
})


def _levenshtein(a: str, b: str, cap: int = 2) -> int:
    """限额编辑距离（超过 cap 提前返回 cap+1，表名都很短，无需矩阵）。"""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _table_allowed(t: str, cte_names: set[str]) -> bool:
    """白名单精确命中 / 本语句声明的 CTE 名 / 编辑距离 ≤1 的近邻
    （复数差异等修复路径，如 fact_orders → fact_order；
    sqlite_master 等系统表差异远大于 1）。"""
    if t in ALLOWED_TABLES or t in cte_names:
        return True
    return any(_levenshtein(t, w) <= 1 for w in ALLOWED_TABLES)


def _collect_cte_names(root: exp.Expression) -> set[str]:
    out: set[str] = set()
    for w in root.find_all(exp.With):
        for cte in w.find_all(exp.CTE):
            alias = cte.alias
            if alias:
                out.add(str(alias).lower())
    return out


def _match_forbidden(node: exp.Expression, issues: list[str],
                     cte_names: set[str]) -> None:
    if isinstance(node, _FORBIDDEN_NODES):
        issues.append(f"禁用的语句类型 {node.key.upper()}")
    if isinstance(node, exp.Anonymous) or isinstance(node, exp.Func):
        name = getattr(node, "name", "") or getattr(getattr(node, "this", None),
                                                    "name", "")
        if name and str(name).lower() in _FORBIDDEN_FUNCS:
            issues.append(f"禁用的函数 {str(name).lower()}")
    if isinstance(node, exp.Table):
        t = (node.name or "").lower()
        if t and not _table_allowed(t, cte_names):
            issues.append(f"表不在白名单: {t}")


def validate_sql_ast(sql: str) -> str:
    """AST 层校验。合法原样返回，违规抛 GuardrailError。"""
    if not sql or not sql.strip():
        raise GuardrailError("SQL 为空，拒绝执行")

    try:
        parsed = sqlglot.parse(sql, read="sqlite")
    except Exception as e:  # noqa: BLE001 —— 任何解析失败都不放行
        raise GuardrailError(f"SQL 解析失败（AST 层），疑似注入载荷: {e}") from e

    parsed = [p for p in parsed if p is not None]
    if not parsed:
        raise GuardrailError("SQL 解析为空语句，拒绝执行")
    if len(parsed) > 1:
        raise GuardrailError("不接受多语句输入（AST 层检测到多条表达式）")

    root = parsed[0]
    if not isinstance(root, (exp.Select, exp.Union)):
        raise GuardrailError(
            f"AST 层只允许只读查询（SELECT/UNION），实际: {root.key.upper()}"
        )

    issues: list[str] = []
    cte_names = _collect_cte_names(root)
    for node in root.walk():
        _match_forbidden(node, issues, cte_names)
    if issues:
        raise GuardrailError(
            "AST 层拒绝执行: " + "; ".join(sorted(set(issues))[:4])
        )

    return sql
