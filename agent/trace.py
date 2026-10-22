"""W5 全链路 Tracing：每一步工具调用、每次检索、每次修正都可回放与定位。

对应 PLAN.md W5「全链路 Tracing」。设计：
  - Tracer 逐事件记录（阶段 / 名字 / 好坏 / 错误 / 耗时），随结果落盘
  - replay() 从文件恢复事件序列回放
  - locate_failure() 定位「是哪一步推理出错」：第一个 failure 事件，
    没有显式 failure 时看对抗 Verifier 是否拦截（其裁定作为独立事件）
"""

from __future__ import annotations

import json
import os
import re
import time

from agent.verifier_adv import _PCT_TAIL, _NUM

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TRACE_DIR = os.path.join(ROOT, "eval", "traces")


class Tracer:
    """一次问答的轨迹收集器（Supervisor 持有，各 Agent 写入）。"""

    def __init__(self, qid: str, trace_dir: str = DEFAULT_TRACE_DIR) -> None:
        self.qid = qid
        self.trace_dir = trace_dir
        self.events: list[dict] = []
        self._t0 = time.time()

    def log(self, phase: str, name: str, ok: bool = True,
            error: str | None = None, detail: dict | None = None) -> None:
        ev = {"t": round(time.time() - self._t0, 3), "phase": phase,
              "name": name, "ok": bool(ok)}
        if error:
            ev["error"] = str(error)
        if detail is not None:
            ev["detail"] = detail
        self.events.append(ev)

    def save(self) -> str:
        os.makedirs(self.trace_dir, exist_ok=True)
        path = os.path.join(self.trace_dir, f"{self.qid or 'run'}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.dump(), f, ensure_ascii=False, indent=2)
        return path

    def dump(self) -> dict:
        return {"qid": self.qid, "n_events": len(self.events),
                "events": self.events}


def replay(path: str) -> list[dict]:
    """从 trace 文件恢复事件序列（回放）。"""
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("events", [])


def locate_failure(path_or_events) -> dict | None:
    """定位第一处推理出错：第一个 ok=False 事件（含 Verifier 拦截）。"""
    events = path_or_events
    if isinstance(path_or_events, str):
        events = replay(path_or_events)
    for ev in events:
        if not ev.get("ok", True):
            return ev
    return None


# --------------------------------------------------------------------------
# W5+：答案数字反查 —— 回答里的每个数字能否追溯回「工具输出证据」
# --------------------------------------------------------------------------

_TOL = 0.01


def _corpus_numbers(corpus: str) -> list[float]:
    return [float(m.group(0).replace(",", ""))
            for m in _NUM.finditer(corpus)
            if _is_num(m.group(0))]


def _is_num(raw: str) -> bool:
    try:
        float(raw.replace(",", ""))
        return True
    except ValueError:
        return False


def trace_numbers(answer: str, tool_outputs: list[str]) -> list[dict]:
    """对答案中出现的每个数字给出溯源标签：
       - corpus_value : 与证据数字同值（容差 1%）
       - derived      : 与两个证据数字的差值/比率一致（环比口径）
       - unknown      : 无法溯源（应被 Verifier 拦下）"""
    corpus = " ".join(tool_outputs or [])
    corpus_nums = _corpus_numbers(corpus)
    corpus_forms: list[float] = []
    for m in _NUM.finditer(corpus):
        raw = m.group(0).replace(",", "")
        if _is_num(raw) and (_PCT_TAIL.match(corpus[m.end():]) or "." in raw):
            corpus_forms.append(float(raw))
    delta_pool, ratio_pool = set(), set()
    for i, a in enumerate(corpus_forms):
        for b in corpus_forms[i + 1:]:
            delta_pool.add(a - b)
            delta_pool.add(b - a)
    for t in tool_outputs or []:
        try:
            b = (json.loads(t) or {}).get("baseline") or {}
        except (ValueError, TypeError):
            continue
        try:
            c, p = float(b.get("current")), float(b.get("previous"))
        except (TypeError, ValueError):
            continue
        if p:
            ratio_pool.add((c - p) / abs(p) * 100.0)
    out = []
    for m in _NUM.finditer(answer):
        raw = m.group(0).replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        if not _is_num(m.group(0)):
            continue
        tol = _TOL * max(1.0, abs(v))
        unit = bool(_PCT_TAIL.match(answer[m.end():]))
        if unit:
            # 与 Verifier 同口径：带单位数字只认「带比率形态」的同值证据
            same = any(abs(v - c) <= _TOL * max(1.0, abs(c))
                       for c in corpus_forms)
        else:
            same = any(abs(v - c) <= _TOL * max(1.0, abs(c))
                       for c in corpus_nums)
        derived = (any(abs(v - d) <= tol or abs(v - abs(d)) <= tol
                       for d in delta_pool | ratio_pool) if unit else False)
        src = ("corpus_value" if same else
               "derived" if derived else "unknown")
        out.append({"value": raw, "unit": unit, "source": src})
    return out


def _tool_kind(raw: str) -> str:
    """tool_output 的证据形态标签。"""
    try:
        d = json.loads(raw) or {}
    except (ValueError, TypeError):
        return "repr"
    if "verifier" in d:
        return "verifier"
    if "memory" in d:
        return "memory"
    if "baseline" in d:
        return "baseline"
    if "time_window" in d:
        return "time_window"
    if "error" in d or "execution_error" in d:
        return "error"
    if "columns" in d or "rows" in d:
        return "sql_result"
    return "other"


def execution_graph(out, qid: str = "") -> dict:
    """把一次回答压缩成可回放的执行图：
       plan → stages（证据形态序列）→ numbers（数字溯源）→ verdict。"""
    stages = [{"kind": _tool_kind(to)} for to in (out.tool_outputs or [])]
    blocked = False
    for to in (out.tool_outputs or []):
        try:
            v = (json.loads(to) or {}).get("verifier")
        except (ValueError, TypeError):
            continue
        if isinstance(v, dict) and v.get("passed") is False:
            blocked = True
    numbers = [] if blocked else trace_numbers(out.answer or "",
                                              out.tool_outputs or [])
    return {
        "qid": qid or getattr(out, "qid", "") or "",
        "error": out.error,
        "steps": len(out.history or []),
        "stages": stages,
        "blocked": blocked,
        "numbers": numbers,
        "n_untraceable": sum(1 for n in numbers if n["source"] == "unknown"),
    }
