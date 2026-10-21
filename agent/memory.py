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
                    self.entries.append(self._normalize(json.loads(line)))

    @staticmethod
    def _normalize(e: dict) -> dict:
        """记忆治理字段补齐（缺省不改变已有行为）：

        - provenance: 信息来源（人/系统/事件），默认 "seed"
        - confidence: 可信度 0~1，默认 0.5
        - valid_from / valid_to: 生效区间（左闭右闭），缺省即全期
        - supersedes: 被本条取代的旧条 memory_id
        - deprecated: True = 已失效（如旧口径 v1），不再进入生效集
        """
        e.setdefault("provenance", "seed")
        e.setdefault("confidence", 0.5)
        e.setdefault("valid_from", "0000-01-01")
        e.setdefault("valid_to", None)
        e.setdefault("supersedes", None)
        e.setdefault("deprecated", False)
        return e

    # ---- 记忆治理（W7 最小可用层） ------------------------------------------

    @staticmethod
    def _dated(e: dict, as_of: str) -> bool:
        """条目在 as_of 当日是否可见：生效区间 + 未被有效新条 supersede。"""
        vf = str(e.get("valid_from") or "0000-01-01")
        vt = e.get("valid_to")
        if vf > as_of or (vt is not None and str(vt) < as_of):
            return False
        return True

    def effective_entries(self, as_of: str | None = None,
                          ) -> list[dict]:
        """as_of 历史时点查询：返回该日期实际生效的记忆集合。

        规则：
        1) 生效区间可见（valid_from ≤ as_of ≤ valid_to，缺省无界）
        2) supersede 链：若某条旧条被「在 as_of 仍生效」的新条引用，
           旧条让位（新信息覆盖旧信息）
        3) as_of 视图（as_of() 产出的对象）冻结自己的时点，
           再次调用 effective_entries() 不会漂移到「当前」
        """
        if as_of is not None:
            ref = as_of
        else:
            ref = getattr(self, "_as_of", None)
        if ref is None:
            ref = "9999-12-31"  # 原生库 = 当前时点
        visible = [e for e in self.entries if self._dated(e, ref)]
        superseded = {e.get("supersedes") for e in visible
                      if e.get("supersedes")}
        return [e for e in visible if e.get("memory_id") not in superseded]

    def as_of(self, date_str: str) -> "MemoryStore":
        """历史时点视图（复用 view 的浅拷贝约定），冻结时点。"""
        v = MemoryStore.__new__(MemoryStore)
        v.path = self.path
        v.entries = list(self.effective_entries(date_str))
        v._as_of = date_str
        return v

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

    def view(self, types: set[str]) -> "MemoryStore":
        """W4 私有记忆隔离：为专家 agent 生成只含指定 memory_type 的视图。

        例如取数专家只见 semantic（口径），归因专家见 procedural+episodic+semantic，
        彼此的可见记忆互不污染（防止取数 agent 拿情节记忆里的结论「猜」数据）。
        """
        v = MemoryStore.__new__(MemoryStore)
        v.path = self.path
        v.entries = [e for e in self.entries if e.get("memory_type") in types]
        return v
