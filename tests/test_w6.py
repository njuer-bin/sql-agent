"""W6 验收：消融表五组复现 / 一键 compare / gold 复核 / 模型路由 fallback / 成本统计。"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PY = sys.executable


# ------------------------------------------------- 验收 3：gold 可复现

def test_build_questions_check_passes():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([PY, os.path.join(ROOT, "eval", "build_questions.py"),
                           "--check"], capture_output=True, text=True,
                          encoding="utf-8", timeout=300, env=env, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr[-400:]
    assert "全部通过" in (proc.stdout + proc.stderr)


# ------------------------------------------------- 验收 2：一键 compare

def test_runner_one_command_compare():
    """`runner.py --compare null rule-baseline ...` 一键出表 + results 落盘。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [PY, os.path.join(ROOT, "eval", "runner.py"),
         "--compare", "null", "rule-baseline", "single-shot", "error-fix",
         "memory", "multi-agent", "--quiet"],
        capture_output=True, text=True, encoding="utf-8", timeout=600,
        env=env, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr[-400:]
    for name in ["null", "rule-baseline", "single-shot", "error-fix",
                 "memory", "multi-agent"]:
        assert os.path.exists(os.path.join(ROOT, "eval", "results",
                                           f"{name}.json"))


# ------------------------------------------------- 验收 1：五组数字可复现

@pytest.mark.parametrize("name,lo", [
    ("null", 0.02), ("rule-baseline", 0.15), ("single-shot", 0.30),
    ("error-fix", 0.35), ("memory", 0.75), ("multi-agent", 0.80),
])
def test_ablation_reproducible(name, lo):
    d = json.load(open(os.path.join(ROOT, "eval", "results", f"{name}.json"),
                       encoding="utf-8"))
    assert d["aggregate"]["final"] >= lo, \
        f"{name} final={d['aggregate']['final']} < {lo}"


def test_multi_agent_answer_target():
    d = json.load(open(os.path.join(ROOT, "eval", "results",
                                    "multi-agent.json"), encoding="utf-8"))
    ag = d["aggregate"]
    assert ag["answer_acc"] >= 0.80
    assert ag["hallucination_rate"] <= 0.05


# ------------------------------------------------- W6：成本统计

def test_aggregate_cost_metrics_present():
    d = json.load(open(os.path.join(ROOT, "eval", "results",
                                    "multi-agent.json"), encoding="utf-8"))
    ag = d["aggregate"]
    for k in ["avg_steps", "avg_tokens", "avg_latency_ms"]:
        assert k in ag, f"缺成本指标 {k}"
    assert ag["avg_steps"] > 0


def test_verdict_carries_cost_fields():
    sys.path.insert(0, os.path.join(ROOT, "eval"))  # eval 模块边界
    from agent.multi_agent import Supervisor
    from agent.tools import ToolBox
    from verifier import score_question  # type: ignore
    v = score_question(
        {"qid": "W6-COST", "category": "a_basic", "question_type": "data",
         "question": "2026 年 9 月我们做了多少 GMV？",
         "expected": {"columns": ["gmv"]}},
        _FakeOut())
    assert v.steps > 0 and v.latency_ms >= 0


class _FakeOut:
    answer = "GMV 是 21,506,250.66（2026-09）"
    sql = "SELECT 1"
    columns = ["gmv"]
    result = [[21506250.66]]
    execution_error = None
    retrieved_memory_ids = []
    tool_outputs = ['{"elapsed_ms": 7}']
    evidence = []
    steps = 2
    tokens = 0
    latency_ms = 0


# ------------------------------------------------- W6：模型路由 fallback

def test_model_router_falls_back_to_offline(monkeypatch):
    import agent.llm as llm
    cfg_bad = llm.LLMConfig()
    cfg_bad.base_url = "http://127.0.0.1:9"
    cfg_bad.api_key = "x"
    cfg_bad.model = "phantom"
    cfg_bad.timeout_sec = 2.0
    r = llm.ModelRouter([cfg_bad, None])
    text, channel = r.complete([{"role": "user", "content": "hi"}])
    assert text is None and channel == "offline"


def test_model_router_uses_primary_when_healthy(monkeypatch):
    import agent.llm as llm
    calls = []

    def fake_chat(messages, cfg=None, temperature=0.0):
        calls.append(cfg.model)
        return "ok"

    monkeypatch.setattr(llm, "chat", fake_chat)
    cfg = llm.LLMConfig()
    cfg.base_url, cfg.api_key, cfg.model = "http://x", "k", "primary"
    cfg2 = llm.LLMConfig()
    cfg2.base_url, cfg2.api_key, cfg2.model = "http://y", "k", "secondary"
    r = llm.ModelRouter([cfg, cfg2])
    text, channel = r.complete([{"role": "user", "content": "hi"}])
    assert text == "ok" and channel == "primary" and calls == ["primary"]


def test_model_router_second_model_on_primary_failure(monkeypatch):
    import agent.llm as llm
    def fake_chat(messages, cfg=None, temperature=0.0):
        if cfg.model == "primary":
            raise RuntimeError("LLM HTTP 500")
        return "from-secondary"

    monkeypatch.setattr(llm, "chat", fake_chat)
    cfg1 = llm.LLMConfig()
    cfg1.base_url, cfg1.api_key, cfg1.model = "http://x", "k", "primary"
    cfg2 = llm.LLMConfig()
    cfg2.base_url, cfg2.api_key, cfg2.model = "http://y", "k", "secondary"
    r = llm.ModelRouter([cfg1, cfg2])
    text, channel = r.complete([{"role": "user", "content": "hi"}])
    assert text == "from-secondary" and channel == "secondary"


def test_model_router_env_default_offline():
    from agent.llm import ModelRouter
    r = ModelRouter()  # 环境无 LLM 配置 → 直接离线
    text, channel = r.complete([{"role": "user", "content": "hi"}])
    assert text is None and channel == "offline"
