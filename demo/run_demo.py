"""5 分钟完整演示（PLAN.md W5 验收 1）：basic / error / memory / attribution / mcp。

python demo/run_demo.py basic|error|memory|attribution|mcp
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

sys.path.insert(0, os.path.join(ROOT, "eval"))  # noqa: E402

from agent.llm import LLMConfig  # noqa: E402
from agent.memory import DEFAULT_MEMORY_PATH  # noqa: E402
from agent.multi_agent import Supervisor  # noqa: E402
from agent.tools import ToolBox  # noqa: E402


def _print_result(qid: str, question: str, out) -> None:
    print(f"\n=== {qid}: {question}")
    print(f"路由后的步数: {out.steps} | 错误: {out.error}")
    if out.sql:
        print(f"SQL: {out.sql}")
    body = (out.answer or "")[:400]
    print(f"结论: {body}")
    for to in out.tool_outputs[:3]:
        print(f"  留痕: {to[:160]}")


def _supervisor() -> Supervisor:
    return Supervisor(ToolBox(), llm_cfg=LLMConfig(),
                      memory_path=DEFAULT_MEMORY_PATH)


def demo_basic() -> None:
    s = _supervisor()
    q = {"qid": "DEMO-BASIC", "question": "2026 年 9 月我们做了多少 GMV？",
         "question_type": "data"}
    _print_result(q["qid"], q["question"], s.answer(q))


def demo_error() -> None:
    """故意喂一条有错列名的 SQL，展示 W2 纠错轨迹。"""
    s = _supervisor()
    q = {"qid": "DEMO-ERROR",
         "question": "2026 年 8 月各地区的 GMV 是多少，按 GMV 从大到小排？",
         "question_type": "data"}
    _print_result(q["qid"], q["question"], s.answer(q))


def demo_memory() -> None:
    s = _supervisor()
    q = {"qid": "DEMO-MEMORY", "question": "客单价到底是怎么算的，分母是什么？",
         "question_type": "memory"}
    _print_result(q["qid"], q["question"], s.answer(q))


def demo_attribution() -> None:
    s = _supervisor()
    q = {"qid": "DEMO-ATTR",
         "question": "2026 年 9 月华北的退款率为什么突然升高了？",
         "question_type": "attribution"}
    _print_result(q["qid"], q["question"], s.answer(q))


def demo_mcp() -> None:
    """独立最小 client 调 MCP server（验收 3/4：调用成功 + 无审批写拦截）。"""
    payload = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "sql_execute", "arguments": {
                        "sql": "SELECT COUNT(*) AS n FROM fact_order"}}}),
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "metric_lookup", "arguments": {
                        "query": "复购率"}}}),
        json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "sql_write", "arguments": {
                        "sql": "INSERT INTO fact_order VALUES (1)", "approved": False}}}),
        json.dumps({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                    "params": {"name": "chart_render", "arguments": {
                        "labels": ["8月", "9月"], "values": [123.4, 156.7],
                        "title": "GMV"}}}),
    ]) + "\n"

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "agent", "mcp_server.py")],
        input=payload, capture_output=True, text=True, encoding="utf-8",
        timeout=60, env=env)
    for line in proc.stdout.strip().splitlines() or []:
        resp = json.loads(line)
        rid = resp.get("id")
        if isinstance(rid, int) and rid == 5:
            r = resp["result"]["content"][0]["json"]
            print(f"MCP id=5 (无审批写): blocked={r.get('blocked')} "
                  f"error={r.get('error')}")
        else:
            print(f"MCP id={rid}: {'ok' if 'result' in resp else 'err'}")
    print("MCP write-block 检查：",
          "approval_required" in proc.stdout)


_DEMOS = {"basic": demo_basic, "error": demo_error, "memory": demo_memory,
          "attribution": demo_attribution, "mcp": demo_mcp}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "basic"
    _DEMOS.get(name, demo_basic)()
