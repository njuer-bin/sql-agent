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
import time

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
