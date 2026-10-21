"""W3 记忆检索：MemoryStore（关键词 + 字符二元组混合检索）。

对应 PLAN.md W3「记忆检索」：
  - 加载 eval/memory_fixtures.jsonl（语义 9 / 情节 6 / 程序 4）
  - 混合检索：关键词整词命中（权重 1.0）+ 字符 bigram 部分命中（≤0.5），
    解决「客单价掉了一截」召回不了「客单价下降」这类词面不一致问题
  - 检索结果作为工具输出留痕（答案里的数字可溯源到记忆条目，防幻觉误判）
  - 口径对齐（A2）：memory adapter 在 SQL 生成前先查记忆，复购率按当前生效
    的 v2 口径（剔除退款用户）执行——nl2sql 的 _repurchase_sql 即 v2 实现
"""

from __future__ import annotations

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MEMORY_PATH = os.path.join(ROOT, "eval", "memory_fixtures.jsonl")


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s).lower()
    return {s[i:i + 2] for i in range(len(s) - 1) if s[i:i + 2].strip()}


class MemoryStore:
    """记忆库：加载 + 混合检索。评测的 memory_fixtures 是 W3 的初始库存。"""

    def __init__(self, path: str = DEFAULT_MEMORY_PATH) -> None:
        self.path = path
        self.entries: list[dict] = []
        if os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                if line.strip():
                    self.entries.append(json.loads(line))

    def retrieve(self, query: str, top_k: int = 6) -> list[dict]:
        """关键词 + bigram 混合检索，返回按相关度排序的 top_k 条。"""
        q_big = _bigrams(query)
        q = re.sub(r"\s+", "", query).lower()
        scored: list[tuple[float, int, dict]] = []
        for idx, e in enumerate(self.entries):
            s = 0.0
            for kw in e.get("keywords", []) or []:
                k = str(kw).lower()
                if k in q:
                    s += 1.0
                else:
                    kb = _bigrams(k)
                    if kb:
                        ov = len(kb & q_big)
                        if ov:
                            s += 0.5 * min(ov / len(kb), 1.0)
            if s > 0:
                scored.append((s, idx, e))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [e for _, _, e in scored[:top_k]]

    def dump(self, entries: list[dict]) -> list[dict]:
        """检索结果转成可留痕的轻量形式（id + 标题 + 正文）。"""
        return [
            {"memory_id": e.get("memory_id"), "title": e.get("title"),
             "content": e.get("content")}
            for e in entries
        ]
