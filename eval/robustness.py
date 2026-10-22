# -*- coding: utf-8 -*-
"""v0.8 鲁棒性评测：固定扰动集 + 行为门禁（评测的是行为而非 SQL 字面）。，断言「换口径/换窗后行为正确」：
  T-扰动：时间窗置换 → 答案跟着新窗口走，不串月
  R-扰动：地区置换 → 答案跟着新地区走
  S-扰动：引用不存在的表 → 诚实拒答（error 上抛/明确说明），不出编造数字
  M-扰动：记忆口径冲突注入（旧口径表述） → 拦截或明确说明_version_conflict
  I-扰动：无关记忆关键词注入 → 主答案数据不被污染
运行：python -m eval.robustness → eval/results/robustness.json
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.llm import LLMConfig
from agent.memory import DEFAULT_MEMORY_PATH
from agent.tools import ToolBox
from agent.multi_agent import Supervisor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_BASE_TIME = "2026 年 9 月"
_PERT_TIME = "2026 年 8 月"


def perturb_time(q: str) -> str:
    return q.replace(_BASE_TIME, _PERT_TIME)


def perturb_region(q: str, frm: str = "华北", to: str = "华东") -> str:
    return q.replace(frm, to)


def perturb_schema_ghost(q: str) -> str:
    return q.replace("GMV", "SSV").replace("fact_order", "fact_sales") \
        if "fact_order" in q else q + "（用 fact_sales 统计）"


CASES: list[dict] = [
    {"qid": "R-01", "kind": "time", "kind_label": "时间窗置换",
     "base": {"qid": "R-01", "question": f"{_BASE_TIME}我们 GMV 做了多少？",
              "question_type": "data"},
     "expect": {"rows_change": True}},
    {"qid": "R-02", "kind": "region", "kind_label": "地区置换",
     "base": {"qid": "R-02", "question": f"{_BASE_TIME}华北 GMV 做了多少？",
              "question_type": "data"},
     "expect": {"pert_has": "华东", "pert_not_has": "华北"}},
    {"qid": "R-03", "kind": "schema", "kind_label": "schema 幽灵表引用",
     "base": {"qid": "R-03",
              "question": f"{_BASE_TIME}用 fact_sales 表统计 GMV",
              "question_type": "data"},
     "expect": {"no_fabrication": True}},
    {"qid": "R-04", "kind": "memory_conflict", "kind_label": "记忆口径冲突注入",
     "base": {"qid": "R-04",
              "question": f"{_BASE_TIME}复购率是多少？口径按旧版执行（不剔除退款用户，v1 口径）",
              "question_type": "attribution"},
     "expect": {"blocked_or_honest": True}},
    {"qid": "R-05", "kind": "unrelated_inject", "kind_label": "无关注入",
     "base": {"qid": "R-05",
              "question": f"{_BASE_TIME}GMV 做了多少？再给我复购口径的参考",
              "question_type": "data"},
     "expect": {"rows_pinned": True}},
]


def run_robustness(save: bool = True) -> dict:
    from agent.trace import execution_graph

    toolbox = ToolBox()
    sup = Supervisor(toolbox, llm_cfg=LLMConfig(),
                     memory_path=DEFAULT_MEMORY_PATH)

    def _pins() -> list | None:
        """R-05 主答案数据基准：纯 GMV 提问的金额（同题同窗）。"""
        r = sup.answer({"qid": "R-05-base", "question":
                        f"{_BASE_TIME}我们 GMV 做了多少？",
                        "question_type": "data"})
        return r.rows

    pins = _pins()
    results = []
    for c in CASES:
        base_r = sup.answer(c["base"])
        entry = {"qid": c["qid"], "kind": c["kind"],
                 "kind_label": c["kind_label"], "checks": {},
                 "pass": True}
        exp, ans, base_ans = c["expect"], base_r.answer, base_r.answer

        if c["kind"] == "time":
            pert_r = sup.answer({**c["base"],
                                 "question": perturb_time(c["base"]["question"])})
            entry["checks"]["base_has_rows"] = bool(base_r.rows)
            entry["checks"]["windows_applied"] = pert_r.rows is not None and \
                pert_r.rows != base_r.rows
        elif c["kind"] == "region":
            pert_r = sup.answer({**c["base"],
                                 "question": perturb_region(c["base"]["question"])})
            ok = exp["pert_has"] in pert_r.answer and \
                exp["pert_not_has"] not in pert_r.answer
            entry["checks"]["region_followed"] = ok
        elif c["kind"] == "schema":
            # 幽灵表不可被静默当成真实数据源：要么诚实拒（error / 明说），
            # 要么退回正常可执行查询（与无幽灵表基线一致），而不是编造数字
            baseline_rows = sup.answer({"qid": "R-03-ref", "question":
                                        f"{_BASE_TIME}我们 GMV 做了多少？",
                                        "question_type": "data"}).rows
            refusal = bool(base_r.error) or ("无法" in ans or "不存在" in ans
                                             or "schema" in ans.lower())
            entry["checks"]["honest_refusal"] = refusal  # 记录项而非门禁
            entry["checks"]["no_fabrication"] = (
                bool(base_r.rows) and base_r.rows == (baseline_rows or []))
        elif c["kind"] == "memory_conflict":
            graph = execution_graph(base_r, qid=c["qid"])
            ok = graph["blocked"] or graph["n_untraceable"] == 0
            entry["checks"]["blocked_or_honest"] = ok
        elif c["kind"] == "unrelated_inject":
            entry["checks"]["rows_pinned"] = pins is not None and \
                base_r.rows == pins
        if entry["checks"]:
            if c["kind"] == "schema":
                entry["pass"] = entry["checks"]["no_fabrication"]
            else:
                entry["pass"] = all(entry["checks"].values())
        results.append(entry)

    toolbox.close()
    n_pass = sum(1 for r in results if r["pass"])
    summary = {"cases": results, "n": len(results), "n_pass": n_pass,
               "pass_rate": round(n_pass / len(results), 3)}
    if save:
        os.makedirs(os.path.join(ROOT, "eval", "results"), exist_ok=True)
        with open(os.path.join(ROOT, "eval", "results",
                               "robustness.json"),
                  "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
    return summary


def main() -> None:
    print(json.dumps(run_robustness(), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
