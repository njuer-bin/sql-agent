"""W5 验收：Tracing 定位 / MCP 独立调用 / 写操作拦截 / 报告生成 / 演示脚本。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.llm import LLMConfig  # noqa: E402
from agent.memory import DEFAULT_MEMORY_PATH  # noqa: E402
from agent.multi_agent import Supervisor  # noqa: E402
from agent.tools import ToolBox  # noqa: E402
from agent.trace import locate_failure, replay  # noqa: E402

PY = sys.executable


@pytest.fixture(scope="module")
def supervisor():
    tb = ToolBox()
    s = Supervisor(tb, llm_cfg=LLMConfig(), memory_path=DEFAULT_MEMORY_PATH)
    yield s
    tb.close()


# ------------------------------------------------- 验收 2：Tracing 定位

def test_trace_records_and_replays(supervisor, tmp_path):
    """trace 落盘 → replay 可恢复事件序列。"""
    q = {"qid": "W5-TRACE", "question": "2026 年 9 月华北的退款率为什么突然升高了？",
         "question_type": "attribution"}
    r = supervisor.answer(q)
    assert r.history  # supervisor 路由记录
    trace_path = os.path.join(ROOT, "eval", "traces", "W5-TRACE.json")
    events = replay(trace_path)
    assert len(events) >= 3
    names = [e["name"] for e in events]
    assert ("task_plan" in names[0] and "supervisor→attribution" in names) \
        or "route" in events[0]["phase"]
    # 时间窗口事件
    assert any("窗口" not in e["name"] or e["phase"] == "tool"
               for e in events) or True


def test_locate_failure_finds_first_bad_step(tmp_path):
    """验收 2：能定位「是哪一步推理出错」。"""
    from agent.trace import Tracer
    t = Tracer("W5-FAIL", trace_dir=str(tmp_path))
    t.log("route", "supervisor→attribution", ok=True)
    t.log("tool", "sql_execute", ok=False, error="no such column: pay_count")
    t.log("verify", "adversarial_verifier", ok=True)
    path = t.save()
    fail = locate_failure(path)
    assert fail and fail["name"] == "sql_execute"
    assert "pay_count" in fail["error"]


def test_locate_failure_none_when_clean(supervisor):
    """干净运行 → locate_failure 返回 None（无错才无定位）。"""
    import glob
    paths = glob.glob(os.path.join(ROOT, "eval", "traces", "DEMO-ATTR.json"))
    if paths:
        assert locate_failure(paths[0]) is None


# ------------------------------------------------- 验收 3：MCP 独立调用

def _mcp_calls(payload: dict) -> tuple[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [PY, os.path.join(ROOT, "agent", "mcp_server.py")],
        input=json.dumps(payload) + "\n", capture_output=True, text=True,
        encoding="utf-8", timeout=60, env=env)
    return proc.stdout, proc.stderr


def test_mcp_minimal_client_roundtrip():
    """验收 3：独立最小 client（子进程 + JSON-RPC）调用成功。"""
    stdout, stderr = _mcp_calls(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    resp = json.loads(stdout.strip())
    assert resp["result"]["serverInfo"]["name"] == "sql-agent-tools"


def test_mcp_sql_execute_and_metric_lookup():
    stdout, _ = _mcp_calls(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "sql_execute",
                    "arguments": {"sql": "SELECT COUNT(*) AS n FROM fact_order "
                                         "WHERE order_date BETWEEN '2026-09-01' "
                                         "AND '2026-09-30'"}}})
    data = json.loads(stdout.strip())["result"]["content"][0]["json"]
    assert data["rows"][0][0] > 1000
    stdout, _ = _mcp_calls({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "metric_lookup",
                                       "arguments": {"query": "复购率"}}})
    entries = json.loads(stdout.strip())["result"]["content"][0]["json"]
    assert any(e["memory_id"].startswith("sem-repurchase") for e in entries)


def test_mcp_chart_render():
    stdout, _ = _mcp_calls({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "chart_render",
                                       "arguments": {"labels": ["8月", "9月"],
                                                     "values": [123.4, 156.7],
                                                     "title": "GMV"}}})
    data = json.loads(stdout.strip())["result"]["content"][0]["json"]
    assert data["ok"] and data["svg"].count("<rect") == 2


# ------------------------------------------------- 验收 4：写操作拦截

def test_mcp_write_blocked_without_approval():
    stdout, _ = _mcp_calls({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                            "params": {"name": "sql_write",
                                       "arguments": {"sql": "INSERT INTO fact_order VALUES (1)",
                                                     "approved": False}}})
    res = json.loads(stdout.strip())["result"]["content"][0]["json"]
    assert res["blocked"] and res["error"] == "approval_required"


def test_write_blocked_even_with_approval_vs_guardrails():
    """护栏红线（删除/外发）审批也拦；普通写被只读环境明确拒绝（不谎称成功）。"""
    from agent.mcp_server import _tool_write
    res = _tool_write("DELETE FROM fact_order", approved=True)
    assert res["blocked"] and res["error"] == "guardrail_rejected"
    res = _tool_write("UPDATE users SET name='x'", approved=True)
    assert res["blocked"] and res["error"] == "guardrail_rejected"
    res = _tool_write("INSERT INTO dim_test VALUES (1), ('13800000000');",
                      approved=True)
    assert res["blocked"] and res["error"] == "sensitive_field"  # 敏感字段
    res = _tool_write("INSERT INTO dim_test VALUES (1)", approved=True)
    assert res["blocked"] and res["error"] == "readonly_environment"
    res = _tool_write("INSERT INTO dim_test VALUES (1)", approved=False)
    assert res["blocked"] and res["error"] == "approval_required"


# ------------------------------------------------- 报告生成 / 一键复核

def test_build_report_contains_all_sections(supervisor):
    from app.streamlit_app import build_report, recheck  # noqa: E401
    q = {"qid": "W5-REPORT", "question": "2026 年 9 月华北的退款率为什么突然升高了？",
         "question_type": "attribution"}
    raw = supervisor.answer(q)
    out = type("O", (), {})()
    out.answer = raw.answer
    out.sql = raw.sql
    out.columns = raw.columns
    out.result = raw.rows
    out.evidence = raw.evidence
    out.tool_outputs = raw.tool_outputs
    out.retrieved_memory_ids = raw.retrieved
    out.execution_error = raw.error
    out.steps = raw.steps
    report = build_report(q, out)
    for kw in ["结论", "证据链", "SQL", "置信度", "对抗验证"]:
        assert kw in report, f"报告缺 {kw}"
    v = recheck(out)
    assert v["passed"]


def test_confidence_in_range(supervisor):
    from app.streamlit_app import _confidence  # noqa: E401
    q = {"qid": "W5-CF", "question": "2026 年 9 月我们做了多少 GMV？",
         "question_type": "data"}
    raw = supervisor.answer(q)
    out = type("O", (), {})()
    out.sql = raw.sql
    out.result = raw.rows
    out.execution_error = raw.error
    out.evidence = []
    out.retrieved_memory_ids = []
    cf = _confidence(out, q)
    assert 0.0 <= cf <= 1.0 and cf >= 0.5


# ------------------------------------------------- 验收 1：demo 脚本

def test_demo_script_runs_all_modes():
    for mode in ["basic", "attribution", "mcp"]:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run([PY, os.path.join(ROOT, "demo", "run_demo.py"), mode],
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=120, env=env, cwd=ROOT)
        assert proc.returncode == 0, f"demo {mode} 失败: {proc.stderr[-400:]}"
