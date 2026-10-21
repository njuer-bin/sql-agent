"""W2 自我纠错循环：错误分类 → 按类型修复 → 重试（上限 3 次）。

对应 PLAN.md W2「SQL 自我纠错循环」：
  错误分类：语法错 / 列不存在 / 表不存在 / 超时 / 空结果
  修复策略：列不存在 → 附真 schema + 列近邻推荐回灌提示；
            空结果 → 放宽时间/状态约束；超时 → 收窄时间窗
  熔断：重试上限 3 次后返回明确的失败原因（A4）

设计约束：
  - 修复是确定性的字符串变换（离线可复现）；LLM 通道只在生成首条 SQL 时使用
  - 护栏错误（敏感字段 / 写操作）是安全红线，**不自动修复**，直接失败
"""

from __future__ import annotations

import difflib
import re
import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.errors import GuardrailError, SqlToolError
from agent.tools import ToolBox

MAX_REPAIRS = 3

# ---------------------------------------------------------------- 错误分类


@dataclass
class ErrorInfo:
    etype: str          # missing_column / missing_table / syntax / timeout / unknown
    token: str | None   # 出错对象（列名 / 表名 / near 片段），可为 None
    raw: str


def classify(msg: str) -> ErrorInfo:
    m = re.search(r"no such column: (.+)", msg, re.I)
    if m:
        return ErrorInfo("missing_column", m.group(1).strip(), msg)
    m = re.search(r"no such table: (.+)", msg, re.I)
    if m:
        return ErrorInfo("missing_table", m.group(1).strip(), msg)
    if "syntax error" in msg.lower() or "incomplete input" in msg.lower() \
            or "unrecognized token" in msg.lower() \
            or "one statement at a time" in msg.lower():
        near = None
        m = re.search(r'near "([^"]+)"', msg, re.I)
        if m:
            near = m.group(1)
        else:
            m = re.search(r'unrecognized token: "([^"]+)"', msg, re.I)
            if m:
                near = m.group(1)
        return ErrorInfo("syntax", near, msg)
    if "超时" in msg or "interrupted" in msg.lower():
        return ErrorInfo("timeout", None, msg)
    return ErrorInfo("unknown", None, msg)


# ---------------------------------------------------------------- schema 目录


def load_catalog(schema_path: str) -> dict[str, list[str]]:
    """从 schema.sql 解析 表名 → 列名列表（运行时解析，不硬编码）。"""
    text = open(schema_path, encoding="utf-8").read()
    catalog: dict[str, list[str]] = {}
    for m in re.finditer(r"CREATE TABLE\s+(\w+)\s*\((.*?)\);", text, re.S):
        cols = re.findall(r"^\s{4}(\w+)\s+(?:INTEGER|TEXT|DATE|NUMERIC|REAL)", m.group(2), re.M)
        catalog[m.group(1).lower()] = cols
    return catalog


def _referenced_tables(sql: str) -> list[str]:
    return [t.lower() for t in re.findall(r"(?:FROM|JOIN)\s+(\w+)", sql, re.I)]


# ---------------------------------------------------------------- 修复策略


def _replace_token(sql: str, old: str, new: str) -> str:
    """整词替换（大小写不敏感；不碰引号内的内容近似即可，离线启发式）。"""
    return re.sub(rf"\b{re.escape(old)}\b", new, sql, flags=re.I)


def _fuzzy_close(name: str, pool: list[str], cutoff: float = 0.7) -> str | None:
    hits = difflib.get_close_matches(name.lower(), [p.lower() for p in pool], n=1, cutoff=cutoff)
    return hits[0] if hits else None


_ALIAS_STOP = {"where", "group", "order", "limit", "join", "on", "and", "or", "as", "set"}


def _alias_map(sql: str, catalog: dict[str, list[str]]) -> dict[str, str]:
    """FROM/JOIN 后的 alias → table 映射（无别名时 table → table）。"""
    out: dict[str, str] = {}
    for m in re.finditer(r"(?:FROM|JOIN)\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?", sql, re.I):
        t = m.group(1).lower()
        a = m.group(2)
        if a and a.lower() not in _ALIAS_STOP:
            out[a.lower()] = t
        elif t in catalog:
            out.setdefault(t, t)
    return out


def repair_missing_column(sql: str, token: str, catalog: dict[str, list[str]]) -> str | None:
    base = token.split(".")[-1]
    # 带引号/含非词字符的"列名"实质是引号噪声 → 走语法修复
    if not re.fullmatch(r"[\w\.]+", base or ""):
        return repair_syntax(sql, None)
    refs = _referenced_tables(sql)
    amap = _alias_map(sql, catalog)
    ref_cols: list[str] = []
    for t in refs:
        ref_cols += catalog.get(t, [])

    def _resolve_token(best: str) -> str:
        """token 带别名前缀时，把前缀纠正到 best 真正所在的表。"""
        if "." not in token:
            return best
        prefix = token.split(".")[0].lower()
        owner = next((t for t, cols in catalog.items()
                      if t in refs and best.lower() in [c.lower() for c in cols]), None)
        if owner and amap.get(prefix) != owner:
            new_alias = next((a for a, t in amap.items() if t == owner and a != t), owner)
            return f"{new_alias}.{best}"
        return f"{prefix}.{best}"

    # 1) 列名**精确**存在于已引用表（拼写没错，前缀错了）→ 直接换
    if base.lower() in [c.lower() for c in ref_cols]:
        return _replace_token(sql, token, _resolve_token(base))

    # 2) 列名**精确**存在于未 JOIN 的维表（跨表取列）→ 注入 JOIN
    for dim, cols in catalog.items():
        if dim in refs or base.lower() not in [c.lower() for c in cols]:
            continue
        key = next((c for c in cols if c.lower().endswith("_id")), None)
        if key is None:
            continue
        # 在已引用的事实表里找同名外键
        for fact in refs:
            fcols = catalog.get(fact, [])
            if key not in fcols:
                continue
            m = re.search(rf"(?:FROM|JOIN)\s+{fact}\s+(?:(\w+)\s+)?", sql, re.I)
            fact_alias = m.group(1) if (m and m.group(1) and m.group(1).lower() not in
                                        _ALIAS_STOP) else fact
            dim_alias = f"{dim[4]}_{len(refs)}" if dim.startswith("dim_") else f"{dim}_x"
            if re.search(rf"\b{re.escape(dim_alias)}\b", sql):
                dim_alias = dim_alias + "2"
            join_sql = f" JOIN {dim} {dim_alias} ON {dim_alias}.{key} = {fact_alias}.{key}"
            # 限定名（o.col）与裸列都要替换，且限定名优先（避免 o.c_1.col 双前缀）
            pat = rf"\b{re.escape(token)}\b|\b{re.escape(base)}\b"
            out = re.sub(pat, f"{dim_alias}.{base}", sql, flags=re.I)
            if " WHERE " in out:
                idx = out.index(" WHERE ")
                out = out[:idx] + join_sql + out[idx:]
            elif re.search(r"\bGROUP BY\b", out, re.I):
                idx = re.search(r"\bGROUP BY\b", out, re.I).start()
                out = out[:idx] + join_sql + out[idx:]
            else:
                out = out.rstrip().rstrip(";") + join_sql
            return out

    # 3) 近邻列（拼写错）：region_name≠region_id 这类近邻陷阱由精确分支先挡掉
    best = _fuzzy_close(base, ref_cols)
    if best:
        return _replace_token(sql, token, _resolve_token(best))
    return None


def repair_missing_table(sql: str, token: str, catalog: dict[str, list[str]]) -> str | None:
    best = _fuzzy_close(token, list(catalog.keys()), cutoff=0.6)
    return _replace_token(sql, token, best) if best else None


_SQL_KEYWORDS = ["SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "LIMIT",
                 "JOIN", "ON", "AND", "OR", "HAVING", "BETWEEN", "AS", "IN", "NOT"]


def repair_syntax(sql: str, near: str | None) -> str | None:
    # 末尾多余分号（多语句尾随 ';'）
    stripped = sql.rstrip().rstrip(";").rstrip()
    if stripped and stripped != sql.strip():
        return stripped
    # 全角标点
    for a, b in [("，", ","), ("；", ";"), ("：", ":"), ("（", "("), ("）", ")")]:
        if a in sql:
            return sql.replace(a, b)
    # 中文引号 → 单引号（必须一次换完，否则残缺引号会破坏时间谓词被护栏拦截）
    out = sql
    for q in ("「", "」", "『", "』", "“", "”", "‘", "’"):
        if q in out:
            out = out.replace(q, "'")
    if out != sql:
        return out
    # FROM / WHERE / GROUP / ORDER / LIMIT / ) 前的悬挂逗号
    out = re.sub(r",\s*(?=(FROM|WHERE|GROUP\s+BY|ORDER\s+BY|LIMIT|\)|;))", " ", sql, flags=re.I)
    if out != sql:
        return out
    # 括号不平衡 → 补右括号
    if sql.count("(") > sql.count(")"):
        return sql + ")" * (sql.count("(") - sql.count(")"))
    # 单引号奇数个 → 补一个
    if sql.count("'") % 2 == 1:
        return sql + "'"
    # 保留字做裸别名：AS order → AS "order"
    out = re.sub(r"\bAS\s+(order|from|where|select|group|limit|on|and|or|join|in)\b(?!\s*\()",
                 r'AS "\1"', sql, flags=re.I)
    if out != sql:
        return out
    # 关键词拼写错：sqlite 的 near 指向坏词后面的 token，所以检查 near 的**前一个词**
    if near:
        m = re.search(rf"\b(\w+)\s+{re.escape(near)}\b", sql, re.I)
        if m and m.group(1).lower() not in ("from", "join", "where", "select", "and", "or", "on"):
            kw = _fuzzy_close(m.group(1), _SQL_KEYWORDS, cutoff=0.6)
            if kw and kw.upper() != m.group(1).upper():
                return _replace_token(sql, m.group(1), kw.upper())
    return None


_DATE_BETWEEN = re.compile(
    r"(BETWEEN\s+')(\d{4}-\d{2}-\d{2})('\s+AND\s+')(\d{4}-\d{2}-\d{2})(')", re.I)

# 数据仓库的时间跨度钳位（W0 验收口径，放宽不得无界外扩）
_MIN_DATE = _dt.date(2025, 1, 1)
_MAX_DATE = _dt.date(2026, 12, 31)


def widen_dates(sql: str) -> str | None:
    """空结果放宽第一步：成倍放宽所有日期窗（保住条件的完整性，不删条件）。

    幅度取 3×跨度和 90 天的较大者，保证 3 次修复预算内必可触达数据区；
    窗口被钳位到 [_MIN_DATE, _MAX_DATE]，若无可放宽则返回 None。
    """

    def _widen(m: re.Match) -> str:
        try:
            d1 = _dt.date.fromisoformat(m.group(2))
            d2 = _dt.date.fromisoformat(m.group(4))
        except ValueError:
            return m.group(0)
        span = max((d2 - d1).days, 0)
        step = max(span * 3, 90)
        nd1 = max(d1 - _dt.timedelta(days=step), _MIN_DATE)
        nd2 = min(d2 + _dt.timedelta(days=step), _MAX_DATE)
        if (nd1, nd2) == (d1, d2):
            return m.group(0)
        return f"{m.group(1)}{nd1.isoformat()}{m.group(3)}{nd2.isoformat()}{m.group(5)}"

    out = _DATE_BETWEEN.sub(_widen, sql)
    return out if out != sql else None


def repair_timeout(sql: str) -> str | None:
    # 把超长时间窗收窄到 7 天（d1 .. d1+6）
    def _narrow(m: re.Match) -> str:
        try:
            d1 = _dt.date.fromisoformat(m.group(2))
            d2 = _dt.date.fromisoformat(m.group(4))
        except ValueError:
            return m.group(0)
        if (d2 - d1).days > 6:
            d2 = d1 + _dt.timedelta(days=6)
            return f"{m.group(1)}{d2.isoformat()}{m.group(5)}"
        return m.group(0)

    out = re.sub(r"(BETWEEN\s+')(\d{4}-\d{2}-\d{2})('\s+AND\s+')(\d{4}-\d{2}-\d{2})(')",
                 _narrow, sql, flags=re.I)
    if out != sql:
        return out
    # 隐式笛卡尔积：FROM t a, t b → 补同日期关联
    m = re.search(r"(FROM\s+fact_order\s+\w+)\s*,\s*(fact_order\s+\w+)", sql, re.I)
    if m:
        a_alias = m.group(1).split()[-1]
        b_alias = m.group(2).split()[-1]
        return sql.replace(m.group(0),
                           f"{m.group(1)} JOIN {m.group(2)} ON {b_alias}.order_date = {a_alias}.order_date")
    return None


def _top_level_where_span(sql: str) -> tuple[int, int] | None:
    m = re.search(r"\bWHERE\b", sql, re.I)
    if not m:
        return None
    start = m.start()
    depth = 0
    for i in range(m.end(), len(sql)):
        c = sql[i]
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                return start, i
            depth -= 1
        elif depth == 0 and re.match(r"(GROUP\s+BY|ORDER\s+BY|LIMIT)\b", sql[i:], re.I):
            return start, i
    return start, len(sql)


_ENUM_COLS = {"order_status", "region_name", "region_id", "channel_name", "channel_type",
              "city", "province", "category_l1", "category_l2", "brand", "gender",
              "age_group", "channel_id", "acquisition_channel_id", "is_resolved"}


def _split_top_conditions(clause: str) -> list[tuple[int, int, str]]:
    """按顶层 AND/OR 把 WHERE 子句切成 (含前导连接词的 start, end, 条件文本)。

    顶层判定要跳过：单引号字符串内的 AND/OR、括号嵌套、以及 BETWEEN ... AND 的
    那个 AND（它属于同一条件，切掉会得到残缺 SQL）。
    """
    depth = 0
    marks: list[tuple[int, int]] = []  # 连接词位置
    in_str = False
    seg_start = 0                      # 上一个顶层连接词结束的位置
    i = 0
    while i < len(clause):
        c = clause[i]
        if in_str:
            if c == "'":
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0:
            m = re.match(r"\b(AND|OR)\b", clause[i:], re.I)
            if m:
                seg = clause[seg_start:i]
                is_between_and = (m.group(1).upper() == "AND"
                                  and re.search(r"\bBETWEEN\b", seg, re.I))
                if not is_between_and:
                    marks.append((i, i + m.end()))
                    i += m.end()
                    seg_start = i
                    continue
        i += 1
    if not marks:  # 单条件
        return [(0, len(clause), clause.strip())]
    spans: list[tuple[int, int, str]] = [
        (0, marks[0][0], clause[: marks[0][0]].strip())
    ]
    for idx, (ms, me) in enumerate(marks):
        ce = marks[idx + 1][0] if idx + 1 < len(marks) else len(clause)
        spans.append((ms, ce, clause[me:ce].strip()))
    return spans


def _cond_priority(cond: str) -> int:
    """0=枚举/维度等值过滤（最可能是错值）1=其他 2=时间过滤（最不该丢）。"""
    c = cond.strip().lower()
    if re.search(r"between|>=|<=|>\s*'|<\s*'|strftime", c):
        return 2
    m = re.match(r"[\w.]+\s*=\s*'[^']*'", c)
    if m:
        col = re.match(r"([\w.]+)\s*=", c).group(1).split(".")[-1]
        if col in _ENUM_COLS:
            return 0
        return 1
    return 1


def repair_empty(sql: str, skip: set[str] | None = None,
                 keep_time: bool = True) -> tuple[str, str] | None:
    """空结果 → 去掉「枚举/维度等值」的顶层条件（最可能是错值）。

    keep_time=True 时保留时间过滤（事实表时间过滤是护栏红线，不能为凑数据而删）。
    返回 (新 SQL, 被去掉的条件文本)。
    """
    span = _top_level_where_span(sql)
    if span is None:
        return None
    s, e = span
    clause = sql[s + 5: e]
    conds = _split_top_conditions(clause)
    skip = skip or set()
    ranked = sorted(enumerate(conds), key=lambda x: (_cond_priority(x[1][2]), x[0]))
    for _, (cs, ce, text) in ranked:
        if keep_time and _cond_priority(text) == 2:
            continue
        key = re.sub(r"\s+", " ", text).lower()
        if key in skip or not text:
            continue
        new_clause = clause[:cs] + clause[ce:]
        new_clause = re.sub(r"^\s*(?:AND|OR)\b", "", new_clause, flags=re.I)
        new_clause = re.sub(r"\b(?:AND|OR)\s*$", "", new_clause, flags=re.I)
        return sql[:s + 5] + new_clause + sql[e:], text
    return None


_DISPATCH: dict[str, Callable[[str, str | None, dict[str, list[str]]], str | None]] = {
    "missing_column": lambda sql, tok, cat: repair_missing_column(sql, tok or "", cat),
    "missing_table": lambda sql, tok, cat: repair_missing_table(sql, tok or "", cat),
    "syntax": lambda sql, tok, cat: repair_syntax(sql, tok),
    "timeout": lambda sql, tok, cat: repair_timeout(sql),
}


# ---------------------------------------------------------------- 纠错循环


def _rows_all_zero(rows: list[list[Any]] | None) -> bool:
    """空结果语义：无行，或聚合行全为 0（COUNT/SUM 永远返回一行，0 即空）。"""
    if rows is None:
        return True
    if not rows:
        return True

    def zero_like(v: Any) -> bool:
        try:
            return float(v) == 0.0
        except (TypeError, ValueError):
            return False

    return all(all(zero_like(v) for v in r) for r in rows)


@dataclass
class RepairOutcome:
    final_sql: str | None = None
    result: dict[str, Any] | None = None
    steps: int = 0
    history: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None


class RepairLoop:
    """W2 被测 agent 的纠错内核：最多 max_repairs 次修复重试，熔断给出明确原因。"""

    def __init__(self, toolbox: ToolBox, max_repairs: int = MAX_REPAIRS) -> None:
        self.tools = toolbox
        self.max_repairs = max_repairs
        self.catalog = load_catalog(toolbox.schema_path)

    def run(self, sql: str) -> RepairOutcome:
        oc = RepairOutcome()
        current = sql
        dropped: set[str] = set()
        for attempt in range(self.max_repairs + 1):
            try:
                r = self.tools.sql_execute(current)
                oc.steps += 1
                if not _rows_all_zero(r["rows"]):
                    oc.final_sql = current
                    oc.result = r
                    return oc
                # 空结果：先放宽时间窗（事实表的时间过滤是护栏红线，不能删），
                # 仍为空再降「枚举/维度等值」条件（时间过滤永远保留）
                fixed = widen_dates(current)
                fix_desc = None
                if fixed is None:
                    res = repair_empty(current, dropped, keep_time=True)
                    if res is not None:
                        fixed, fix_desc = res
                oc.history.append({
                    "attempt": attempt + 1, "error_type": "empty_result",
                    "error_msg": "查询返回空（0 行 / 全 0 聚合）",
                    "fix": (f"时间窗放宽" if fix_desc is None else f"去掉条件 {fix_desc}")
                            if fixed else "无法放宽",
                    "sql": fixed,
                })
                if fixed is None or fixed == current:
                    return self._fail(oc, current,
                                      "空结果：时间窗已到数据边界且无状态/维度条件可放宽，"
                                      "查询条件可能与数据时间跨度矛盾")
                current = fixed
                if fix_desc:
                    dropped.add(re.sub(r"\s+", " ", fix_desc).lower())
            except GuardrailError as e:
                oc.steps += 1
                oc.final_sql = current
                # 安全红线：护栏错误不自动修复
                return self._fail(oc, current, f"护栏拦截（安全红线，不自动修复）：{e}")
            except SqlToolError as e:
                oc.steps += 1
                msg = str(e)
                info = classify(msg)
                if attempt == self.max_repairs:
                    return self._fail(oc, current, f"重试预算用尽（{info.etype}）：{msg}")
                fixed = _DISPATCH.get(info.etype, lambda *a: None)(current, info.token, self.catalog) \
                    if info.etype != "unknown" else None
                oc.history.append({
                    "attempt": attempt + 1, "error_type": info.etype, "error_msg": msg[:200],
                    "fix": _fix_desc(info.etype) if fixed else "无法构造修复", "sql": fixed,
                })
                if info.etype == "unknown" or fixed is None or fixed == current:
                    return self._fail(oc, current, f"无法自动修复 {info.etype}：{msg}")
                current = fixed
        return self._fail(oc, current, "重试预算用尽")

    @staticmethod
    def _fail(oc: RepairOutcome, sql: str, reason: str) -> RepairOutcome:
        oc.final_sql = sql
        oc.error = reason
        return oc


def _fix_desc(etype: str) -> str:
    return {
        "missing_column": "列近邻替换 / 补 JOIN / 引号修正",
        "missing_table": "表名近邻替换",
        "syntax": "语法修正（标点/括号/引号/关键词）",
        "timeout": "收窄时间窗或补关联条件",
    }.get(etype, etype)
