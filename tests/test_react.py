"""ReAct 循环：步数预算 / 熔断语义 / 离线确定性计划。"""

import pytest
from agent.errors import SqlToolError
from agent.react import ReActAgent
from agent.tools import ToolBox


@pytest.fixture(scope="module")
def tools() -> ToolBox:
    t = ToolBox()
    yield t
    t.close()


def test_offline_full_pipeline(tools: ToolBox):
    agent = ReActAgent(tools)
    r = agent.run("2026 年 9 月我们做了多少 GMV？")
    assert r.sql is not None
    assert r.steps <= 3
    assert r.tool_calls[-1]["action"] == "sql_execute"
    assert "21,506,250.66" in r.answer
    assert "取数失败" not in r.answer


def test_unsolvable_question_honest(tools: ToolBox):
    agent = ReActAgent(tools)
    r = agent.run("为什么上个月的复购率跌了？")
    assert "不确定" in r.answer or "不支持" in r.answer


def test_budget_enforced_when_tool_error(tools: ToolBox):
    """离线模式工具报错必须熔断给原因，而不是抛异常给用户。"""
    import unittest.mock as mock

    agent = ReActAgent(tools)
    with mock.patch.object(
        ToolBox, "sql_execute", side_effect=SqlToolError("SQL 执行失败: boom")
    ):
        r = agent.run("2026 年 9 月我们做了多少 GMV？")
    assert r.error is not None
    assert r.steps <= 3
    assert "失败" in r.answer
    assert "boom" in r.answer


def test_max_steps_never_exceeded(tools: ToolBox):
    """设置 max_steps=1 时，复杂多轮计划必须以熔断原因收场。"""
    import unittest.mock as mock

    agent = ReActAgent(tools, max_steps=1)
    # 单步预算里把 metric_lookup 也烧掉 → 无法执行 SQL → 熔断
    with mock.patch.object(
        ReActAgent, "_offline_plan", return_value={"action": "metric_lookup", "args": {"query": "gmv"}}
    ):
        r = agent.run("2026 年 9 月我们做了多少 GMV？")
    assert r.error == "max_tool_calls_exceeded"
    assert "上限" in r.answer
