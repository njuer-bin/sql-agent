"""W7 记忆治理层（最小可用）：provenance/confidence/validity/supersedes/as_of。

对应 v0.7 backlog 第 3 条：旧口径失效、新口径覆盖、as_of 历史时点查询。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.memory import MemoryStore, DEFAULT_MEMORY_PATH  # noqa: E402


def _store() -> MemoryStore:
    return MemoryStore(DEFAULT_MEMORY_PATH)


def test_governance_fields_normalized():
    for e in _store().entries:
        assert "provenance" in e and "confidence" in e
        assert "valid_from" in e and e["valid_from"]
        assert "valid_to" in e
        assert "supersedes" in e
        assert isinstance(e["confidence"], (int, float))


def test_current_period_supersede_hides_old():
    ids = [e["memory_id"] for e in _store().effective_entries()]
    assert "sem-repurchase-v2" in ids          # 生效新口径
    assert "sem-repurchase-v1" not in ids      # 被 v2 覆盖（supersedes 链）
    assert "pr-repurchase-v2-rule" in ids      # 独立程序记忆仍在
    v2 = next(e for e in _store().entries
              if e["memory_id"] == "sem-repurchase-v2")
    assert v2["supersedes"] == "sem-repurchase-v1"
    prov = v2["provenance"]
    # 溯源：既有结构化 {source, confidence, ref}，也兼容字符串
    conf = prov.get("confidence") if isinstance(prov, dict) else prov
    assert conf in ("user_wang", "grounded", 0.9) or isinstance(conf, str)


def test_as_of_historical_query_shows_old_version():
    hist = _store().as_of("2026-05-15").effective_entries()
    ids = [e["memory_id"] for e in hist]
    assert "sem-repurchase-v1" in ids          # 历史 v1 当时尚生效
    assert "sem-repurchase-v2" not in ids      # v2 尚未生效（valid_from 09-01）
    # 视图冻结时点：再次 effective_entries 不漂移到「当前」
    assert hist == _store().as_of("2026-05-15").effective_entries()
    # v1 在历史时点的失效边界：valid_to = 2026-08-31
    ids_eom = [e["memory_id"] for e in _store().as_of("2026-08-31").effective_entries()]
    assert "sem-repurchase-v1" in ids_eom
    ids_nov = [e["memory_id"] for e in _store().as_of("2026-09-30").effective_entries()]
    assert "sem-repurchase-v2" in ids_nov
    assert "sem-repurchase-v1" not in ids_nov


def test_current_period_no_dangling_supersede_reference():
    ids = {e["memory_id"] for e in _store().effective_entries()}
    index = {e["memory_id"] for e in _store().entries}
    for e in _store().effective_entries():
        sup = e.get("supersedes")
        # 生效条目引用的「被取代者」必须是真实存在的条目（不留悬空引用）
        assert sup is None or sup in index
