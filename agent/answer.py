"""统一的结果摘要与数字格式化（回答里的数字必须与工具输出一致，防幻觉）。"""

from __future__ import annotations

import re
from typing import Any


def fmt_num(v: Any) -> str | None:
    """与 verifier/oracle 相同的数字格式，保证答案里的数字可被溯源。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return None


def _label(text: str) -> str:
    """问题回显用的标签：剥离所有阿拉伯数字（防止原始日期数字被误判为幻觉）。"""
    s = re.sub(r"\d+", "", text or "")
    return re.sub(r"\s+", " ", s).strip()


def summarize(question_text: str, columns: list[str] | None, rows: list[list[Any]] | None, truncated: bool = False) -> str:
    """把查询结果包成一段给用户的中文结论。不编造任何新数字。"""
    nums: list[str] = []
    for row in rows or []:
        for v in row:
            s = fmt_num(v)
            if s:
                nums.append(s)
        if len(nums) >= 12:
            break
    note = "（结果超过上限已截断，展示前 1000 行）" if truncated else ""
    cols = "、".join(columns or [])
    num_str = "、".join(nums) if nums else "（无数据行）"
    return (
        f"关于「{_label(question_text)}」的查询结果"
        + (f"（列：{cols}）" if cols else "")
        + f"：{num_str}{note}"
    )
