"""SQL 安全护栏（Guardrails）。

三条硬规则（对应 PLAN.md W1 Guardrails，验收 A3/A4 的实现）：
  G1 只允许只读：仅 SELECT/WITH 开头；拒绝写操作、多语句注入、危险关键词
  G2 禁止全表扫描：事实表查询必须带时间过滤条件，否则拦截并提示补条件
  G3 敏感字段：查询中出现个人敏感信息字段时直接拒绝执行
"""

from __future__ import annotations

import re

from agent.errors import GuardrailError

# 事实表 → 必须过滤的时间列
FACT_TABLES: dict[str, str] = {
    "fact_order": "order_date",
    "fact_service_ticket": "created_date",
    "fact_web_traffic": "stat_date",
    "fact_marketing_campaign": "start_date",
}

# 写操作 / 连接劫持类危险关键词（在剥离字符串字面量与注释后的骨架上匹配）
_DANGEROUS_PATTERNS: list[str] = [
    r"\bDROP\b", r"\bDELETE\b", r"\bINSERT\b", r"\bUPDATE\b", r"\bMERGE\b",
    r"\bTRUNCATE\b", r"\bALTER\b", r"\bCREATE\b", r"\bREPLACE\b",
    r"\bATTACH\b", r"\bDETACH\b", r"\bPRAGMA\b", r"\bVACUUM\b",
    r"\bREINDEX\b", r"\bGRANT\b", r"\bREVOKE\b",
]

# 敏感字段模式：骨架级匹配，已在剥壳阶段剔除字符串字面量，误伤可控
_SENSITIVE_FIELD_RE = re.compile(
    r"\b(id_card|idcard|identity_card|身份证|phone|mobile|telephone|手机号|手机号码"
    r"|bank_card|bankcard|银行卡|card_no|email|邮箱|address|住址|home_addr|"
    r"real_name|真实姓名|contact_name)\b",
    re.IGNORECASE,
)

_SINGLE_LINE_COMMENT = re.compile(r"--[^\n]*")
_MULTI_LINE_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`|\[[^\]]*\]")


def _strip_for_check(sql: str) -> str:
    """剥掉注释与字符串字面量只留骨架：
    防止写操作藏在字符串字面量里绕过，也防止业务文本里的 'DROP' 造成误报。"""
    s = _SINGLE_LINE_COMMENT.sub(" ", sql)
    s = _MULTI_LINE_COMMENT.sub(" ", s)
    s = _QUOTED.sub("''", s)
    return s


def _head(sql: str, limit: int = 64) -> str:
    return " ".join(sql.strip().split())[:limit]


def _time_filter_present(skeleton: str, date_col: str) -> bool:
    """检测是否有针对日期列的过滤：显式比较运算 / BETWEEN / IN / strftime 月分桶。"""
    p1 = r"\b[\w\.]*\b" + date_col + r"\b\s*(BETWEEN|>=|<=|<|>|=|IN\b)"
    if re.search(p1, skeleton, re.IGNORECASE):
        return True
    p2 = r"\bstrftime\s*\(\s*'%Y-%m'\s*,\s*[\w\.]*\b" + date_col + r"\b\s*\)\s*(BETWEEN|>=|<=|<|>|=|IN\b)"
    return bool(re.search(p2, skeleton, re.IGNORECASE))


def validate_sql(sql: str) -> str:
    """校验一条 SQL 是否可以放行执行。合法返回原语句，不合法抛 GuardrailError。"""
    if not sql or not sql.strip():
        raise GuardrailError("SQL 为空，拒绝执行")

    skeleton = _strip_for_check(sql)
    head = skeleton.strip().upper()

    # G1a 只读白名单
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        raise GuardrailError(
            f"只允许只读查询（SELECT/WITH），拒绝执行: {_head(sql)}"
        )

    # G1b 多语句注入：剥壳后出现分号且分号后还有实质内容
    body = skeleton.strip().rstrip(";")
    if ";" in body and body.split(";", 1)[1].strip():
        raise GuardrailError(
            f"不接受多语句输入（疑似 SQL 注入），拒绝执行: {_head(sql)}"
        )

    # G1c 写操作 / 危险关键词
    for pat in _DANGEROUS_PATTERNS:
        m = re.search(pat, skeleton, re.IGNORECASE)
        if m:
            word = re.sub(r"^\\b|\\b$", "", pat.split(" ")[0])
            raise GuardrailError(
                f"检测到写操作/危险语句（{m.group(0).strip().upper()}），"
                f"工具层拒绝执行: {_head(sql)}"
            )

    # G3 敏感字段
    sf = _SENSITIVE_FIELD_RE.search(skeleton)
    if sf:
        raise GuardrailError(f"检测到敏感字段 {sf.group(1)}，工具层拒绝执行")

    # G2 全表扫描拦截：语句涉及的每张事实表都必须带各自时间列的过滤
    for table, date_col in FACT_TABLES.items():
        if re.search(rf"\b{table}\b", skeleton, re.IGNORECASE):
            if not _time_filter_present(skeleton, date_col):
                raise GuardrailError(
                    "全表扫描已拦截：查询涉及事实表 "
                    f"{table}，但未检测到对时间列 {date_col} 的过滤条件，"
                    "请先补时间范围"
                    f"（如 WHERE {date_col} BETWEEN '2026-09-01' AND '2026-09-30'）"
                )

    return sql
