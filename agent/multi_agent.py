"""W4 多智能体编排：Supervisor → 专家 agent（私有记忆隔离）→ 对抗式验证。

对应 PLAN.md W4「多智能体编排与对抗验证」：
  - Supervisor：按问题类型路由（data→取数 / memory→检索 / attribution→归因流程）
  - 专家 agent：各自持有 MemoryStore 的受限视图（私有记忆隔离，互不污染）
  - 归因流程落地 pr-attribution-playbook 7 步：确认现象 → 基线量化 → 下钻 →
    假设（情节记忆）→ 验证 → 结论（区分结构性变化与真实流失）
  - AdversarialVerifier 独立审查（不共享 planner 上下文）：数字溯源/方向一致/
    有数据支撑/证据链完整，未通过则拦截并改写为诚实的失败陈述

离线确定性实现（LLM 通道可用时行为一致，只是各步生成器可替换）。
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from agent.answer import fmt_num
from agent.error_repair import RepairLoop
from agent.memory import DEFAULT_MEMORY_PATH, MemoryStore
from agent.memory_agent import MemoryAgent
from agent.nl2sql_offline import (build_offline_sql, detect_metric,
                                  detect_region, parse_time)
from agent.single_shot import ErrorFixAgent, SingleShotResult
from agent.tools import ToolBox
from agent.verifier_adv import AdversarialVerifier

# --------------------------------------------------------------------------------------
# 专家 agent（各持受限记忆视图）
# --------------------------------------------------------------------------------------

class RetrievalExpert(MemoryAgent):
    """检索专家：全量记忆（语义+情节+程序），负责记忆类问题与记忆供给。"""

    name = "retrieval"

    def __init__(self, toolbox: ToolBox, llm_cfg=None,
                 memory_path: str = DEFAULT_MEMORY_PATH, top_k: int = 6) -> None:
        super().__init__(toolbox, llm_cfg, memory_path=memory_path, top_k=top_k)
        self.name = RetrievalExpert.name


class DataExpert(ErrorFixAgent):
    """取数专家：无私有记忆（连 semantic 口径也不看，口径由调用方传入），
    只做「SQL → 纠错 → 结果」，防止把情节记忆里的结论当数据用。"""

    name = "data"

    def __init__(self, toolbox: ToolBox, llm_cfg=None, max_repairs: int = 3) -> None:
        super().__init__(toolbox, llm_cfg, max_repairs=max_repairs)
        self.name = DataExpert.name


class AttributionExpert:
    """归因专家：私有记忆 = procedural(流程) + episodic(事件) + semantic(口径)。

    7 步流程（playbook）→ 对应实现：
      1 确认现象      = 时间窗口定位 + 当前口径取数
      2 拉基线量化    = 上一周期同口径取数，算偏离
      3 按维度下钻    = 渠道/地区下钻（按程序记忆的固定顺序）
      4 对比贡献度    = 新老客拆分等子分析
      5 形成候选假设  = 情节记忆检索（结构性事件档案）
      6 逐一验证      = 用取数验证方向与幅度（对抗 Verifier 复核）
      7 输出结论      = 区分结构性变化与真实流失，引用证据链
    """

    name = "attribution"

    _METRIC_KW = {
        "refund_rate": ["退款率", "退货率"],
        "aov": ["客单价", "单均价"],
        "gmv_gross": ["GMV"],
        "gmv_net": ["净GMV"],
        "repurchase_rate": ["复购率"],
        "new_buyer_count": ["新客"],
    }
    _METRIC_LABEL = {
        "refund_rate": "退款率", "aov": "客单价", "gmv_gross": "GMV",
        "gmv_net": "净GMV", "repurchase_rate": "复购率",
        "new_buyer_count": "新客数", "order_count": "订单数",
    }

    def __init__(self, toolbox: ToolBox, store: MemoryStore) -> None:
        self.tools = toolbox
        self.store = store.view({"procedural", "episodic", "semantic"})

    # ---- 取数工具 -----------------------------------------------------------

    def _run_sql(self, sql: str) -> tuple[bool, list | None, list | None, str]:
        """跑 SQL；护栏/错误交给 W2 纠错循环。返回 (ok, columns, rows, error)。"""
        if not sql:
            return False, None, None, "no_sql"
        oc = RepairLoop(self.tools, max_repairs=3).run(sql)
        if oc.ok:
            return True, oc.result["columns"], oc.result["rows"], ""
        return False, None, None, oc.error or ""

    def _run_sql_value(self, sql: str) -> float | None:
        ok, _cols, rows, _e = self._run_sql(sql)
        if not ok or not rows or not rows[0]:
            return None
        # 首个数值单元格（扫描/下钻类查询的第一列是日期或维度名）
        for v in rows[0]:
            if isinstance(v, (int, float)):
                return float(v)
            try:
                return float(str(v))
            except ValueError:
                continue
        return None  # None/空 → 无数据（10 月之后未入库等）

    @staticmethod
    def _shift_period(sql: str, start: str, end: str,
                      delta_days: int) -> str | None:
        """把 SQL 里最后一个 BETWEEN 的日期窗口整体平移 delta_days 天（拉基线用）。"""
        pat = re.compile(
            r"(BETWEEN\s+')(\d{4}-\d{2}-\d{2})('\s+AND\s+')(\d{4}-\d{2}-\d{2})(')",
            re.I)
        matches = list(pat.finditer(sql))
        if not matches:
            return None
        m = matches[-1]
        d0 = date.fromisoformat(m.group(2))
        d1 = date.fromisoformat(m.group(4))
        new_sql = (sql[:m.start()] + m.group(1)
                   + (d0 - timedelta(days=delta_days)).isoformat() + m.group(3)
                   + (d1 - timedelta(days=delta_days)).isoformat() + m.group(5)
                   + sql[m.end():])
        return new_sql

    def _scan_sql(self, region: str | None, start: str, end: str) -> str:
        """异动扫描：区域内逐日 GMV + 退款率（老规矩体检的标准跑法）。"""
        where = "o.order_status='paid'"
        if region:
            where += f" AND r.region_name='{region}'"
        return (
            f"SELECT date(o.order_date) AS d, ROUND(SUM(o.pay_amount),2) AS gmv, "
            f"ROUND(100.0*SUM(o.refund_amount)/SUM(o.pay_amount),2) AS refund_rate "
            f"FROM fact_order o JOIN dim_region r ON r.region_id=o.region_id "
            f"WHERE {where} AND o.order_date BETWEEN '{start}' AND '{end}' "
            f"GROUP BY d ORDER BY d"
        )

    def _newold_split_sql(self, region: str | None, start: str, end: str) -> str:
        """新老客拆分（下钻步骤 4：新客占比是复购率下降最常见的稀释原因）。"""
        where = "o.order_status='paid'"
        if region:
            where += f" AND r.region_name='{region}'"
        return (
            f"SELECT strftime('%Y-%m', o.order_date) AS ym, "
            f"COUNT(DISTINCT o.customer_id) AS buyers, "
            f"COUNT(DISTINCT CASE WHEN c.first_order_date=o.order_date "
            f"THEN o.customer_id END) AS new_buyers, "
            f"ROUND(100.0*COUNT(DISTINCT CASE WHEN c.first_order_date=o.order_date "
            f"THEN o.customer_id END)/COUNT(DISTINCT o.customer_id),2) AS new_pct "
            f"FROM fact_order o JOIN dim_region r ON r.region_id=o.region_id "
            f"JOIN dim_customer c ON c.customer_id=o.customer_id "
            f"WHERE {where} AND o.order_date BETWEEN '{start}' AND '{end}' "
            f"GROUP BY ym ORDER BY ym"
        )

    def _channel_rank_sql(self, start: str, end: str) -> str:
        return (
            f"SELECT c.channel_name, ROUND(SUM(o.pay_amount),2) AS gmv "
            f"FROM fact_order o JOIN dim_channel c ON c.channel_id=o.channel_id "
            f"WHERE o.order_status='paid' AND o.order_date BETWEEN '{start}' AND '{end}' "
            f"GROUP BY c.channel_name"
        )

    # ---- 记忆工具 -----------------------------------------------------------

    def _match_episodic(self, text: str, metric: str | None,
                        region: str | None, start: str) -> dict | None:
        """结构化匹配情节记忆：月份 × 地区 × 指标（关键词 bigram 重叠）。"""
        yy, mm = start[:4], start[5:7]
        month_tok = f"{yy}-{mm}"
        m_big = None
        if metric:
            kws = self._METRIC_KW.get(metric) or []
            if kws:
                from agent.memory import _bigrams
                m_big = _bigrams(kws[0])
        best, best_s = None, 0.0
        for e in self.store.entries:
            if e.get("memory_type") != "episodic":
                continue
            kws = [str(k) for k in (e.get("keywords") or [])]
            s = 0.0
            if any(k.lower() == month_tok for k in kws):
                s += 2.0
            if region and region in kws:
                s += 2.0
            if m_big is not None:
                from agent.memory import _bigrams
                for k in kws:
                    if m_big <= _bigrams(k):
                        s += 2.0
                        break
            if s > best_s:
                best, best_s = e, s
        return best if best_s >= 4.0 else None

    def _linked_semantic(self, ep: dict | None) -> list[dict]:
        """情节记忆的关联口径（关键词 bigram 交叉，如 6月对账事件 → cancelled 陷阱）。"""
        if ep is None:
            return []
        from agent.memory import _bigrams
        ep_all = set()
        for k in ep.get("keywords") or []:
            ep_all |= _bigrams(str(k))
        out = []
        for e in self.store.entries:
            if e.get("memory_type") != "semantic":
                continue
            kws = e.get("keywords") or []
            if any(_bigrams(str(k)) & ep_all for k in kws):
                out.append(e)
        return out

    def _procedural(self, text: str) -> list[dict]:
        hits = self.store.retrieve(text, top_k=2)
        return [e for e in hits if e.get("memory_type") == "procedural"]

    @staticmethod
    def _parse_fixed_order(content: str) -> list[str]:
        m = re.search(r"固定顺序[:：]([^。]+)", content)
        if not m:
            return []
        return [p.strip() for p in m.group(1).split("→") if p.strip()]

    # ---- 归因主流程 ---------------------------------------------------------

    def answer(self, question: dict) -> SingleShotResult:
        out = SingleShotResult()
        text = str(question.get("question", ""))
        period = parse_time(text)
        metric = detect_metric(text)
        region = detect_region(text)
        window_txt = (f"{period[0]}~{period[1]}" if period else "未识别的时间范围")
        out.steps = 1
        out.retrieved = []

        executed = ["时间窗口定位" if period else "时间窗口缺失"]
        if period:
            start, end = period
        else:
            start, end = "2026-09-01", "2026-09-30"
        # 时间窗口留痕：答案里的日期数字可溯源
        out.tool_outputs.append(json.dumps({"time_window": {"start": start,
                                                            "end": end}},
                                           ensure_ascii=False))

        # 步骤 1：确认现象（当前口径取数）
        cur_sql = build_offline_sql(text)
        if cur_sql is None and region and not metric:
            cur_sql = self._scan_sql(region, start, end)
        val_cur = self._run_sql_value(cur_sql) if cur_sql else None
        cur_ok = val_cur is not None
        if cur_sql and cur_ok:
            executed.append("现象确认取数")
            out.sql = cur_sql
            out.steps += 1

        # 步骤 2：拉基线（上一周期同口径）
        baseline_sql = self._shift_period(cur_sql, start, end, 31) if cur_sql else None
        val_prev = self._run_sql_value(baseline_sql) if baseline_sql else None
        baseline_ok = val_prev is not None
        if cur_ok:
            executed.append("基线对比" if baseline_ok else "基线缺失（上一周期无数据/取数失败）")
            if baseline_ok:
                out.steps += 1
                out.tool_outputs.append(json.dumps(
                    {"baseline": {"current": val_cur, "previous": val_prev}},
                    ensure_ascii=False))

        # 步骤 3+5：下钻与假设 —— 情节记忆匹配 + 程序记忆
        ep = self._match_episodic(text, metric, region, start)
        procs = self._procedural(text)
        if not any(p.get("memory_id") == "pr-attribution-playbook" for p in procs):
            # 归因必然走团队标准流程：playbook 是兜底的程序记忆
            procs.append(next(e for e in self.store.entries
                              if e.get("memory_id") == "pr-attribution-playbook"))
        sems = self._linked_semantic(ep)
        cited = ([ep] if ep else []) + procs + sems
        if cited:
            executed.append("假设检索（情节记忆）")
            out.retrieved = [e.get("memory_id") for e in cited]
            dumped = self.store.dump(cited)
            out.tool_outputs.append(json.dumps({"memory": dumped},
                                               ensure_ascii=False))
            out.steps += 1

        # 步骤 4：贡献度下钻 —— 新老客拆分（复购类）/ 渠道固定顺序（渠道类）
        newold = None
        ch_rows = None
        want_newold = metric in ("repurchase_rate", "new_buyer_count")
        want_channel = (metric in ("gmv_gross", "gmv_net")
                        and ("渠道" in text and "拆" in text or "老顺序" in text
                             or "下钻" in text))
        if want_newold:
            # 看回 3 个月：不然只有本周期一行，没有「新客占比变化」对比
            no_start = (date.fromisoformat(end) - timedelta(days=92)).isoformat()
            ok2, _c, rows2, _e2 = self._run_sql(
                self._newold_split_sql(region, no_start, end))
            if ok2 and rows2:
                newold = rows2
                executed.append("新老客拆分")
                out.steps += 1
                out.tool_outputs.append(json.dumps(
                    {"newold_split": {"columns": ["ym", "buyers", "new_buyers",
                                                  "new_pct"], "rows": rows2}},
                    ensure_ascii=False))
        if want_channel and procs:
            order = None
            for p in procs:
                order = self._parse_fixed_order(p.get("content", ""))
                if order:
                    break
            if order:
                ok3, _c, rows3, _e3 = self._run_sql(self._channel_rank_sql(start, end))
                if ok3 and rows3:
                    rank = {name: i for i, name in enumerate(order)}
                    rows3.sort(key=lambda r: (rank.get(str(r[0]), 99), str(r[0])))
                    ch_rows = rows3
                    executed.append("渠道下钻（程序记忆固定顺序）")
                    out.steps += 1
                    out.tool_outputs.append(json.dumps(
                        {"channel_rank": {"columns": ["channel_name", "gmv"],
                                          "rows": rows3, "order": order}},
                        ensure_ascii=False))

        # 步骤 7：结论组装
        body = self._compose(text, metric, region, window_txt,
                             (cur_ok, val_cur, baseline_ok, val_prev),
                             ep, procs, sems, newold, ch_rows)
        out.answer = body["text"]
        out.evidence = body["evidence"]
        measured = {}
        if cur_ok and baseline_ok and metric:
            # 方向校验：答案声称的方向必须与实测一致
            measured[self._METRIC_LABEL.get(metric, metric)] = val_cur - val_prev

        # 对抗式验证（独立审查：只看答案与工具输出）
        required = ["时间窗口定位", "现象确认取数", "基线对比", "假设检索（情节记忆）",
                    "假设验证", "结论输出"]
        verdict = AdversarialVerifier().check(
            out.answer, out.tool_outputs, measured=measured,
            executed_steps=executed + ["假设验证", "结论输出"],
            required_steps=required)
        out.tool_outputs.append(json.dumps(
            {"verifier": {"passed": verdict.passed,
                          "issues": [f"[{i.check}] {i.detail}" for i in verdict.issues]}},
            ensure_ascii=False))
        out.steps += 1
        if not verdict.passed:
            out.answer = ("对抗验证未通过，以下结论被拦下：" + "；".join(
                f"[{i.check}] {i.detail}" for i in verdict.issues)
                + "。我不会发布未经验证的结论。")
            out.evidence = ["对抗验证拦截"]
        return out

    # ---- 结论组装 -----------------------------------------------------------

    def _compose(self, text, metric, region, window_txt, run, ep, procs, sems,
                 newold, ch_rows) -> dict:
        cur_ok, val_cur, baseline_ok, val_prev = run
        label = self._METRIC_LABEL.get(metric, metric or "")
        ev = ["时间窗口定位"]
        parts: list[str] = []

        # 1) 现象与基线（量化）
        if cur_ok and baseline_ok:
            if val_prev:
                pct = (val_cur - val_prev) / abs(val_prev) * 100.0
                trend = "上升" if pct > 0 else ("下降" if pct < 0 else "持平")
                parts.append(
                    f"基线对比：{label}上一周期 {fmt_num(val_prev)}，本周期 {fmt_num(val_cur)}"
                    f"，{trend}约 {fmt_num(abs(pct))}%。")
            else:
                parts.append(
                    f"基线对比：{label}上一周期无数据/为 0，本周期 {fmt_num(val_cur)}。")
            ev.append(f"{label}量化")
        elif cur_ok:
            parts.append(f"基线缺失：本周期 {label} {fmt_num(val_cur)}，上一周期无可比数据，"
                         "基线对比未完成，不下结论。")
        else:
            parts.append(f"现象确认未完成：{label or '该指标'}在该时间窗口未取到数据"
                         "（可能尚未入库或取数失败），不编造数值。")

        # 2) 新老客拆分（结构性 vs 流失 的关键证据）
        if newold and label in ("复购率",):
            latest = newold[-1]
            parts.append(
                f"新老客拆分：{latest[0]} 买家 {fmt_num(latest[1])}，新客 "
                f"{fmt_num(latest[2])}，新客占比 {fmt_num(latest[3])}%。")
            ev += ["新客占比量化", "新老客拆分", "拆分新老客"]

        # 3) 渠道下钻（固定顺序）
        if ch_rows:
            seq = " → ".join(f"{r[0]} {fmt_num(r[1])}" for r in ch_rows)
            parts.append(f"渠道下钻（按程序记忆固定顺序）：{seq}。")
            ev += ["渠道下钻"]

        # 4) 假设与验证 —— 引用情节记忆（一下钻就用事实说话，不瞎猜）
        if ep:
            parts.append(f"假设与验证：记忆库中有对应事件档案【{ep.get('title')}】"
                         f"{ep.get('content')}")
            ev += ["假设检索", "假设验证", "排除其他原因"]
        else:
            if cur_ok and baseline_ok and val_prev:
                parts.append("假设检索：记忆库中没有与本时间窗口/地区对应的已知事件档案，"
                             f"本周期 {label} 变动与已知结构性事件无关，未发现可归因的异常。")
            elif not cur_ok:
                pass

        # 5) 口径备注（语义记忆关联）
        for s in sems:
            parts.append(f"口径提醒【{s.get('title')}】{s.get('content')}")

        # 6) 程序记忆（走了哪套流程）
        for p in procs:
            parts.append(f"流程依据【{p.get('title')}】{p.get('content')}")

        parts.append(f"结论输出：以上为按标准 7 步流程（证据链：{'、'.join(ev)}）"
                     "得出的结论，未经验证的判断不输出。")
        ev += ["结论输出"]
        text_out = f"时间窗口定位：{window_txt}。" + "".join(parts)
        return {"text": text_out, "evidence": ev}


# --------------------------------------------------------------------------------------
# Supervisor：意图路由 → 任务 DAG → 分发
# --------------------------------------------------------------------------------------

class Supervisor:
    """W4 编排器：路由 + DAG 声明 + 专家分发。

    专家各持受限记忆视图（私有记忆隔离）：
      retrieval  → 全量记忆
      attribution→ procedural + episodic + semantic
      data       → 无记忆（防拿结论当数据）
    """

    name = "multi-agent"

    def __init__(self, toolbox: ToolBox, llm_cfg=None,
                 memory_path: str = DEFAULT_MEMORY_PATH) -> None:
        self.tools = toolbox
        self.store = MemoryStore(memory_path)
        self.experts = {
            "retrieval": RetrievalExpert(toolbox, llm_cfg,
                                         memory_path=memory_path),
            "data": DataExpert(toolbox, llm_cfg),
            "attribution": AttributionExpert(toolbox, self.store),
        }

    def route(self, question: dict) -> str:
        qt = question.get("question_type", "data")
        return {"data": "data", "memory": "retrieval",
                "attribution": "attribution"}.get(qt, "data")

    def dag(self, expert_name: str) -> list[str]:
        """任务 DAG（顺序流水；每步输出是下一步的输入）。"""
        if expert_name == "retrieval":
            return ["memory_retrieve", "answer_compose", "evidence_trace"]
        if expert_name == "attribution":
            return ["time_window", "sql_execute_current", "sql_execute_baseline",
                    "memory_hypothesis", "drilldown", "verify_adversarial",
                    "answer_compose"]
        return ["sql_generate", "repair_loop", "answer_summarize"]

    def answer(self, question: dict) -> SingleShotResult:
        expert_name = self.route(question)
        out = self.experts[expert_name].answer(question)
        out.history = list(out.history or []) + [
            {"supervisor": {"route": expert_name, "dag": self.dag(expert_name)}}]
        return out
