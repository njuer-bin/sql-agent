"""W6 成本统计助手：从 agent 产物汇总 tokens / 步数 / 延迟。

口径（文档化）：
  - latency_ms：SQL 工具输出里 elapsed_ms 的总和（真实墙钟）
  - tokens：估算值 = (SQL 生成提示 + 工具留痕 + 答案) 字符数 / 4（LLM 真实
    计费数在启用 LLM 通道时由 llm.chat 返回并覆盖）；离线也给出可比口径
  - steps：agent 返回的步数（检索/执行/验证各计其步）
"""

from __future__ import annotations

import re

_ELAPSED = re.compile(r'"elapsed_ms":\s*([\d.]+)')


def fill_cost(out) -> None:
    """填写 AgentOutput 的 latency_ms / tokens（steps 由 agent 自己给出）。"""
    elapsed = 0.0
    chars = 0
    for t in out.tool_outputs or []:
        s = str(t)
        chars += len(s)
        m = _ELAPSED.search(s)
        if m:
            elapsed += float(m.group(1))
    chars += len(out.sql or "") + len(out.answer or "")
    if out.latency_ms == 0:
        out.latency_ms = int(elapsed)
    if out.tokens == 0:
        out.tokens = chars // 4
