"""工具层错误类型。

GuardrailError  —— SQL 被 guardrails 拦截（安全事件，不允许降级重试）
SqlToolError    —— SQL 合法但执行失败（语法错/列不存在/超时等，W2 纠错循环的输入）
"""

from __future__ import annotations


class GuardrailError(Exception):
    """SQL 违反安全护栏被拦截。"""


class SqlToolError(Exception):
    """SQL 在执行阶段失败。"""
