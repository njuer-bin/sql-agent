#!/usr/bin/env python
"""
评测集生成器
============================================================================
从 warehouse.db 真实数据计算出每道题的 gold 答案，输出到 eval/questions/*.jsonl。

为什么用生成器而不是手写题目：
  1. gold 答案必须和数据库一致，手写必然漂移
  2. 换数据集（换业务）时只需重跑一次
  3. 题目里的 SQL 陷阱（如 cancelled 未清零）能被自动校验

用法：
    python eval/build_questions.py            # 生成
    python eval/build_questions.py --check    # 生成并自校验（gold SQL 可执行 + 结果非空）
============================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date

from verifier import has_affirmative

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, "data", "warehouse", "warehouse.db")
OUT = os.path.join(HERE, "questions")

# --------------------------------------------------------------------------
# 题目分类定义
# --------------------------------------------------------------------------
CATEGORIES = {
    "a_basic":        "单表取数",
    "b_join":         "多表关联",
    "c_metric":       "口径题（语义记忆）",
    "d_episode":      "复用题（情节记忆）",
    "e_procedure":    "套路题（程序记忆）",
    "f_conflict":     "冲突题（冲突消解）",
    "g_attribution":  "归因推理",
    "h_long_horizon": "长程记忆",
}

# 评分权重：不同题型考察点不同
WEIGHTS = {
    "data":        {"execution": 0.15, "schema": 0.15, "result": 0.35, "answer": 0.35},
    "memory":      {"retrieval": 0.30, "answer": 0.70},
    "attribution": {"execution": 0.10, "result": 0.30, "answer": 0.45, "evidence": 0.15},
}


def run_sql(con, sql, params=()):
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return cols, [list(r) for r in cur.fetchall()]


def gold_from_sql(con, sql, ordered=False, limit=None):
    """执行 gold SQL，返回 expected 结构。"""
    cols, rows = run_sql(con, sql)
    if limit:
        rows = rows[:limit]
    return {
        "columns": cols,
        "result": rows,
        "ordered": ordered,
        "tolerance": 0.01,
    }


# --------------------------------------------------------------------------
# 题目定义
#   kind = "data"        → 有 SQL gold
#       = "memory"      → 只有文本 gold（key facts）
#       = "attribution" → SQL gold + 文本 gold
# --------------------------------------------------------------------------

def build(con):
    Q = []
    counters: dict[str, int] = {}

    def add(cat, kind, question, difficulty, session, turn,
            memory_types=None, ambiguity="", gold_sql=None,
            ordered=False, limit=None, answer_contains=None,
            answer_forbidden=None, must_mention=None,
            evidence_required=None, note=""):
        counters[cat] = counters.get(cat, 0) + 1
        qid = f"{cat[0].upper()}-{counters[cat]:03d}"
        item = {
            "qid": qid,
            "category": cat,
            "category_name": CATEGORIES[cat],
            "question_type": kind,
            "difficulty": difficulty,
            "session_id": session,
            "turn": turn,
            "question": question,
            "requires_memory": memory_types or [],
            "ambiguity": ambiguity,
            "expected": {},
            "scoring": {"weights": WEIGHTS[kind]},
            "note": note,
        }
        if gold_sql:
            item["expected"].update(gold_from_sql(con, gold_sql, ordered, limit))
            item["expected"]["reference_sql"] = " ".join(gold_sql.split())
        if answer_contains:
            item["expected"]["answer_contains"] = answer_contains
        if answer_forbidden:
            item["expected"]["answer_forbidden"] = answer_forbidden
        if must_mention:
            item["expected"]["must_mention"] = must_mention
        if evidence_required:
            item["expected"]["evidence_required"] = evidence_required
        Q.append(item)
        return item

    # =====================================================================
    # A. 单表取数
    # =====================================================================
    add("a_basic", "data",
        "2026 年 9 月我们做了多少 GMV？", "easy", "S01", 1,
        gold_sql="""
            SELECT ROUND(SUM(pay_amount),2) AS gmv
            FROM fact_order
            WHERE order_status='paid' AND order_date BETWEEN '2026-09-01' AND '2026-09-30'
        """,
        answer_contains=["GMV"],
        note="基础题，但埋了 cancelled 陷阱 —— 若不过滤 order_status 会偏高")

    add("a_basic", "data",
        "2026 年 9 月一共下了多少单？", "easy", "S01", 2,
        gold_sql="""
            SELECT COUNT(*) AS order_cnt FROM fact_order
            WHERE order_status='paid' AND order_date BETWEEN '2026-09-01' AND '2026-09-30'
        """)

    add("a_basic", "data",
        "2026 年 9 月各渠道分别做了多少 GMV？按 GMV 从高到低排。", "easy", "S01", 3,
        gold_sql="""
            SELECT c.channel_name, ROUND(SUM(o.pay_amount),2) AS gmv
            FROM fact_order o JOIN dim_channel c ON c.channel_id=o.channel_id
            WHERE o.order_status='paid' AND o.order_date BETWEEN '2026-09-01' AND '2026-09-30'
            GROUP BY c.channel_name ORDER BY gmv DESC
        """, ordered=True)

    add("a_basic", "data",
        "2026 年 9 月的退款率是多少？", "easy", "S02", 1,
        gold_sql="""
            SELECT ROUND(100.0*SUM(refund_amount)/SUM(pay_amount),2) AS refund_rate
            FROM fact_order
            WHERE order_status='paid' AND order_date BETWEEN '2026-09-01' AND '2026-09-30'
        """,
        answer_contains=["退款率"])

    add("a_basic", "data",
        "2026 年 9 月 1 日到 9 月 15 日，日均 GMV 是多少？", "easy", "S02", 2,
        gold_sql="""
            SELECT ROUND(SUM(pay_amount)/15.0,2) AS avg_daily_gmv
            FROM fact_order
            WHERE order_status='paid' AND order_date BETWEEN '2026-09-01' AND '2026-09-15'
        """)

    add("a_basic", "data",
        "2026 年至今，哪个月的 GMV 最高？", "medium", "S02", 3,
        gold_sql="""
            SELECT strftime('%Y-%m', order_date) AS ym, ROUND(SUM(pay_amount),2) AS gmv
            FROM fact_order
            WHERE order_status='paid' AND order_date >= '2026-01-01'
            GROUP BY ym ORDER BY gmv DESC LIMIT 1
        """, ordered=True)

    add("a_basic", "data",
        "2026 年 9 月客服一共收到多少工单？", "easy", "S03", 1,
        gold_sql="""
            SELECT COUNT(*) AS tickets FROM fact_service_ticket
            WHERE created_date BETWEEN '2026-09-01' AND '2026-09-30'
        """)

    add("a_basic", "data",
        "2026 年 9 月已解决工单的平均解决时长是多少小时？", "easy", "S03", 2,
        gold_sql="""
            SELECT ROUND(AVG(resolve_hours),2) AS avg_hours FROM fact_service_ticket
            WHERE is_resolved=1 AND created_date BETWEEN '2026-09-01' AND '2026-09-30'
        """)

    # =====================================================================
    # B. 多表关联
    # =====================================================================
    add("b_join", "data",
        "2026 年 9 月各区域的 GMV 是多少？按 GMV 降序。", "easy", "S04", 1,
        gold_sql="""
            SELECT r.region_name, ROUND(SUM(o.pay_amount),2) AS gmv
            FROM fact_order o JOIN dim_region r ON r.region_id=o.region_id
            WHERE o.order_status='paid' AND o.order_date BETWEEN '2026-09-01' AND '2026-09-30'
            GROUP BY r.region_name ORDER BY gmv DESC
        """, ordered=True)

    add("b_join", "data",
        "2026 年 9 月各一级类目的 GMV 占比情况。", "medium", "S04", 2,
        gold_sql="""
            SELECT p.category_l1, ROUND(SUM(o.pay_amount),2) AS gmv,
                   ROUND(100.0*SUM(o.pay_amount)/(SELECT SUM(pay_amount) FROM fact_order
                        WHERE order_status='paid' AND order_date BETWEEN '2026-09-01' AND '2026-09-30'),2) AS pct
            FROM fact_order o JOIN dim_product p ON p.product_id=o.product_id
            WHERE o.order_status='paid' AND o.order_date BETWEEN '2026-09-01' AND '2026-09-30'
            GROUP BY p.category_l1 ORDER BY gmv DESC
        """, ordered=True)

    add("b_join", "data",
        "2026 年 9 月华东区各渠道的 GMV。", "easy", "S04", 3,
        gold_sql="""
            SELECT c.channel_name, ROUND(SUM(o.pay_amount),2) AS gmv
            FROM fact_order o
            JOIN dim_region r ON r.region_id=o.region_id
            JOIN dim_channel c ON c.channel_id=o.channel_id
            WHERE o.order_status='paid' AND o.order_date BETWEEN '2026-09-01' AND '2026-09-30'
              AND r.region_name='华东'
            GROUP BY c.channel_name ORDER BY gmv DESC
        """, ordered=True)

    add("b_join", "data",
        "2026 年 9 月各区域的新客数是多少？", "medium", "S05", 1,
        gold_sql="""
            SELECT r.region_name, COUNT(DISTINCT c.customer_id) AS new_buyers
            FROM dim_customer c JOIN dim_region r ON r.region_id=c.region_id
            WHERE c.first_order_date BETWEEN '2026-09-01' AND '2026-09-30'
            GROUP BY r.region_name ORDER BY new_buyers DESC
        """, ordered=True)

    add("b_join", "data",
        "2026 年 9 月复购率最高的三个区域是哪些？", "hard", "S05", 2,
        gold_sql="""
            WITH per_user AS (
                SELECT o.region_id, o.customer_id, COUNT(*) AS n, SUM(o.refund_amount) AS ref
                FROM fact_order o
                WHERE o.order_status='paid' AND o.order_date BETWEEN '2026-09-01' AND '2026-09-30'
                GROUP BY o.region_id, o.customer_id
            ),
            agg AS (
                SELECT region_id,
                       100.0*COUNT(CASE WHEN n>=2 THEN 1 END)/COUNT(*) AS rep_rate
                FROM per_user WHERE ref=0 GROUP BY region_id
            )
            SELECT r.region_name, ROUND(a.rep_rate,2) AS repurchase_rate
            FROM agg a JOIN dim_region r ON r.region_id=a.region_id
            ORDER BY a.rep_rate DESC LIMIT 3
        """, ordered=True,
        note="这题同时考 v2 口径（剔除退款用户）")

    add("b_join", "data",
        "2026 年 9 月各渠道的退款率。", "medium", "S05", 3,
        gold_sql="""
            SELECT c.channel_name,
                   ROUND(100.0*SUM(o.refund_amount)/SUM(o.pay_amount),2) AS refund_rate
            FROM fact_order o JOIN dim_channel c ON c.channel_id=o.channel_id
            WHERE o.order_status='paid' AND o.order_date BETWEEN '2026-09-01' AND '2026-09-30'
            GROUP BY c.channel_name ORDER BY refund_rate DESC
        """, ordered=True)

    add("b_join", "data",
        "2026 年第三季度（7-9 月）各月的 GMV 趋势。", "easy", "S06", 1,
        gold_sql="""
            SELECT strftime('%Y-%m', order_date) AS ym, ROUND(SUM(pay_amount),2) AS gmv
            FROM fact_order
            WHERE order_status='paid' AND order_date BETWEEN '2026-07-01' AND '2026-09-30'
            GROUP BY ym ORDER BY ym
        """, ordered=True)

    add("b_join", "data",
        "2026 年 9 月华东区 GMV 最高的 5 个城市。", "medium", "S06", 2,
        gold_sql="""
            SELECT r.city, ROUND(SUM(o.pay_amount),2) AS gmv
            FROM fact_order o JOIN dim_region r ON r.region_id=o.region_id
            WHERE o.order_status='paid' AND o.order_date BETWEEN '2026-09-01' AND '2026-09-30'
              AND r.region_name='华东'
            GROUP BY r.city ORDER BY gmv DESC LIMIT 5
        """, ordered=True, limit=5)

    # =====================================================================
    # C. 口径题（语义记忆）
    # =====================================================================
    add("c_metric", "memory",
        "复购率到底怎么算的？", "medium", "S07", 1,
        memory_types=["semantic"], ambiguity="复购率的窗口、是否剔除退款用户都有过变更",
        answer_contains=["下单次数", "2", "去重用户数", "剔除退款"],
        must_mention=["剔除退款", "净下单"],
        note="考 v2 口径；答成 v1（不剔除退款）应判错")

    add("c_metric", "memory",
        "客单价的分母到底是订单数还是下单用户数？", "easy", "S07", 2,
        memory_types=["semantic"], ambiguity="业务方口头说的客单价常指人均消费",
        answer_contains=["订单数"],
        must_mention=["订单数"],
        answer_forbidden=["用户数做分母", "分母是用户数"])

    add("c_metric", "memory",
        "算 GMV 的时候要不要把退款扣掉？", "easy", "S07", 3,
        memory_types=["semantic"], ambiguity="毛 GMV 与净 GMV 是两个指标",
        answer_contains=["毛", "不扣", "净"],
        must_mention=["毛"])

    add("c_metric", "memory",
        "新客是按注册时间算还是按首单时间算？", "easy", "S08", 1,
        memory_types=["semantic"], ambiguity="注册和首购口径差异很大",
        answer_contains=["首单", "首次下单"],
        must_mention=["首次下单"])

    add("c_metric", "memory",
        "退款率是按金额算还是按单量算？", "easy", "S08", 2,
        memory_types=["semantic"], ambiguity="两种算法结果差异明显",
        answer_contains=["金额"],
        must_mention=["金额"])

    add("c_metric", "memory",
        "会话数这个指标确认过口径了吗？", "hard", "S08", 3,
        memory_types=["semantic"], ambiguity="该指标在字典里是未确认状态",
        answer_contains=["未确认", "没有确认", "尚未确认"],
        must_mention=["未确认"],
        note="考置信度分级 —— 应该回答「未确认，需向流量组确认」而不是编一个口径")

    # =====================================================================
    # D. 复用题（情节记忆）
    # =====================================================================
    add("d_episode", "memory",
        "上个月华东复购率下降那事儿，最后查出来是什么原因？", "medium", "S09", 1,
        memory_types=["episodic"],
        answer_contains=["新客占比", "结构性", "不是老客流失"],
        must_mention=["新客占比", "结构性"],
        note="必须命中 2026-10-16 的归因结论")

    add("d_episode", "memory",
        "8 月华东那边客单价掉了一截，是什么原因来着？", "medium", "S09", 2,
        memory_types=["episodic"],
        answer_contains=["补贴退坡", "到期"],
        must_mention=["补贴退坡"])

    add("d_episode", "memory",
        "9 月华北退款率突然升高是怎么回事？", "medium", "S09", 3,
        memory_types=["episodic"],
        answer_contains=["物流", "WMS", "发货延迟"],
        must_mention=["物流"])

    add("d_episode", "memory",
        "今年 6 月我们的 GMV 为什么跟财务对不上？", "hard", "S10", 1,
        memory_types=["episodic", "semantic"],
        answer_contains=["cancelled", "已取消", "pay_amount", "未清零"],
        must_mention=["已取消"],
        note="考「事件记忆 + 口径记忆」的联合召回")

    add("d_episode", "memory",
        "3 月华东自营 GMV 暴涨那件事，当时的结论是什么？", "easy", "S10", 2,
        memory_types=["episodic"],
        answer_contains=["大促", "促销"],
        must_mention=["大促"])

    add("d_episode", "memory",
        "抖音渠道 4 月开始的增长是什么原因？", "easy", "S10", 3,
        memory_types=["episodic"],
        answer_contains=["主播", "带货"],
        must_mention=["主播"])

    # =====================================================================
    # E. 套路题（程序记忆）
    # =====================================================================
    add("e_procedure", "attribution",
        "老规矩，帮我看看 9 月华东有没有什么异动。", "hard", "S11", 1,
        memory_types=["procedural", "episodic", "semantic"],
        ambiguity="「老规矩」= 团队标准归因流程，agent 必须知道这个流程",
        gold_sql="""
            SELECT date(o.order_date) AS d, ROUND(SUM(o.pay_amount),2) AS gmv,
                   ROUND(100.0*SUM(o.refund_amount)/SUM(o.pay_amount),2) AS refund_rate
            FROM fact_order o JOIN dim_region r ON r.region_id=o.region_id
            WHERE o.order_status='paid' AND r.region_name='华东'
              AND o.order_date BETWEEN '2026-09-01' AND '2026-09-30'
            GROUP BY d ORDER BY d
        """,
        answer_contains=["无异常", "正常", "未发现"],
        must_mention=["无异常"],
        evidence_required=["基线对比"],
        note="华东 9 月无已知效应，正确答案是「无异常」—— 考 agent 会不会为了凑答案而编造")

    add("e_procedure", "attribution",
        "按标准流程帮我分析一下 10 月华东的复购率情况。", "hard", "S11", 2,
        memory_types=["procedural", "semantic", "episodic"],
        ambiguity="标准流程 7 步；复购率用 v2 口径",
        gold_sql="""
            SELECT strftime('%Y-%m', o.order_date) AS ym,
                   COUNT(DISTINCT o.customer_id) AS buyers,
                   COUNT(DISTINCT CASE WHEN c.first_order_date=o.order_date
                                       THEN o.customer_id END) AS new_buyers
            FROM fact_order o
            JOIN dim_region r ON r.region_id=o.region_id
            JOIN dim_customer c ON c.customer_id=o.customer_id
            WHERE o.order_status='paid' AND r.region_name='华东'
              AND o.order_date BETWEEN '2026-07-01' AND '2026-10-15'
            GROUP BY ym ORDER BY ym
        """,
        answer_contains=["新客占比", "结构性"],
        must_mention=["新客占比", "结构性"],
        evidence_required=["新老客拆分", "基线对比"])

    add("e_procedure", "attribution",
        "渠道那边按老顺序拆一下 9 月的 GMV。", "medium", "S11", 3,
        memory_types=["procedural"],
        ambiguity="「老顺序」= 固定的渠道下钻顺序",
        gold_sql="""
            SELECT c.channel_name, ROUND(SUM(o.pay_amount),2) AS gmv
            FROM fact_order o JOIN dim_channel c ON c.channel_id=o.channel_id
            WHERE o.order_status='paid' AND o.order_date BETWEEN '2026-09-01' AND '2026-09-30'
            GROUP BY c.channel_name
            ORDER BY CASE c.channel_name
                     WHEN '自营APP' THEN 1 WHEN '自营小程序' THEN 2
                     WHEN '天猫旗舰店' THEN 3 WHEN '京东自营' THEN 4
                     WHEN '抖音旗舰店' THEN 5 WHEN '线下门店' THEN 6 ELSE 7 END
        """, ordered=True,
        must_mention=["自营APP"],
        note="顺序由程序记忆决定，不是按 GMV 排序")

    add("e_procedure", "memory",
        "9 月的复购率帮我算一下，记得用现在该用的那个口径。", "hard", "S12", 1,
        memory_types=["procedural", "semantic"],
        ambiguity="「现在该用的口径」= v2（剔除退款用户），且 9 月正好是 v2 生效的第一个月",
        gold_sql="""
            WITH per_user AS (
                SELECT customer_id, COUNT(*) AS n, SUM(refund_amount) AS ref
                FROM fact_order
                WHERE order_status='paid' AND order_date BETWEEN '2026-09-01' AND '2026-09-30'
                GROUP BY customer_id
            )
            SELECT ROUND(100.0*COUNT(CASE WHEN n>=2 THEN 1 END)/COUNT(*),2) AS repurchase_rate
            FROM per_user WHERE ref=0
        """,
        answer_contains=["复购率"],
        must_mention=["剔除退款"],
        note="9 月是 v2 生效首月，用 v1 口径会算出不同的数")

    # =====================================================================
    # F. 冲突题（冲突消解）
    # =====================================================================
    add("f_conflict", "memory",
        "我记得复购率是不用剔除退款用户的，现在还是这样吗？", "hard", "S13", 1,
        memory_types=["semantic", "procedural"],
        ambiguity="用户记忆是 v1，但当前生效的是 v2 —— 必须识别冲突并消解",
        answer_contains=["不是", "已变更", "剔除退款", "2026-09"],
        must_mention=["剔除退款", "2026-09"],
        answer_forbidden=["不用剔除", "还是不用"],
        note="核心冲突题：不能附和用户，也不能生硬否定，要给出变更时间点")

    add("f_conflict", "memory",
        "2026 年 8 月的复购率应该用哪个口径算？", "medium", "S13", 2,
        memory_types=["semantic"],
        ambiguity="8 月 31 日是 v1 的最后一天，9 月 1 日是 v2 第一天",
        answer_contains=["v1", "不剔除"],
        must_mention=["v1"],
        note="边界月份 —— 考版本生效区间判断")

    add("f_conflict", "memory",
        "我要跨 9 月做复购率的趋势对比，有什么要注意的？", "medium", "S13", 3,
        memory_types=["procedural", "semantic"],
        ambiguity="v1 和 v2 口径不可比",
        answer_contains=["口径不可比", "口径不一致", "需要还原", "不可比"],
        must_mention=["不可比"],
        note="考冲突的下游影响，不只是识别冲突")

    add("f_conflict", "memory",
        "客单价我一直说是用用户数做分母的吧？", "hard", "S14", 1,
        memory_types=["semantic"],
        ambiguity="用户记错了 —— 口径是订单数，且两者相差 1.8 倍",
        answer_contains=["订单数", "不是用户数"],
        must_mention=["订单数"],
        answer_forbidden=["对，用用户数", "是的，用户数"],
        note="考 agent 会不会为了讨好用户而附和错误记忆")

    # =====================================================================
    # G. 归因推理
    # =====================================================================
    add("g_attribution", "attribution",
        "2026 年 9 月华北的退款率为什么突然升高了？", "hard", "S15", 1,
        memory_types=["episodic", "procedural"],
        gold_sql="""
            SELECT date(o.order_date) AS d,
                   ROUND(100.0*SUM(o.refund_amount)/SUM(o.pay_amount),2) AS refund_rate,
                   COUNT(*) AS orders
            FROM fact_order o JOIN dim_region r ON r.region_id=o.region_id
            WHERE o.order_status='paid' AND r.region_name='华北'
              AND o.order_date BETWEEN '2026-09-01' AND '2026-09-30'
            GROUP BY d ORDER BY d
        """,
        answer_contains=["物流", "发货延迟", "WMS"],
        must_mention=["物流"],
        evidence_required=["时间窗口定位", "退款率量化", "排除其他原因"],
        note="必须定位到 9/10~9/14 这个窗口，并给出退款率从 1.21% 升到 6.00%")

    add("g_attribution", "attribution",
        "2026 年 8 月华东天猫和京东的客单价为什么下降了？", "hard", "S15", 2,
        memory_types=["episodic", "semantic"],
        gold_sql="""
            SELECT strftime('%Y-%m', o.order_date) AS ym,
                   ROUND(AVG(o.pay_amount),2) AS aov, COUNT(*) AS orders
            FROM fact_order o
            JOIN dim_region r ON r.region_id=o.region_id
            JOIN dim_channel c ON c.channel_id=o.channel_id
            WHERE o.order_status='paid' AND r.region_name='华东'
              AND c.channel_name IN ('天猫旗舰店','京东自营')
              AND o.order_date BETWEEN '2026-06-01' AND '2026-10-15'
            GROUP BY ym ORDER BY ym
        """,
        answer_contains=["补贴退坡", "补贴", "到期"],
        must_mention=["补贴"],
        evidence_required=["客单价量化", "排除抖音渠道", "订单量未变"],
        note="必须指出不含抖音，否则是把两个效应混在一起")

    add("g_attribution", "attribution",
        "2026 年 6 月我们的 GMV 为什么比财务口径高？", "hard", "S15", 3,
        memory_types=["episodic", "semantic", "procedural"],
        gold_sql="""
            SELECT order_status, COUNT(*) AS cnt, ROUND(SUM(pay_amount),2) AS total_pay,
                   ROUND(100.0*SUM(pay_amount)/(SELECT SUM(pay_amount) FROM fact_order
                        WHERE order_date BETWEEN '2026-06-01' AND '2026-06-30'),2) AS pct
            FROM fact_order
            WHERE order_date BETWEEN '2026-06-01' AND '2026-06-30'
            GROUP BY order_status
        """,
        answer_contains=["已取消", "cancelled", "未清零"],
        must_mention=["已取消"],
        evidence_required=["量化高估幅度", "给出修复方式"],
        note="这是数据质量陷阱题，考 agent 会不会真的去查 status 分布")

    add("g_attribution", "attribution",
        "2026 年 3 月华东自营的 GMV 为什么暴涨？", "medium", "S16", 1,
        memory_types=["episodic"],
        gold_sql="""
            SELECT strftime('%Y-%m', o.order_date) AS ym, ROUND(SUM(o.pay_amount),2) AS gmv,
                   COUNT(*) AS orders
            FROM fact_order o
            JOIN dim_region r ON r.region_id=o.region_id
            JOIN dim_channel c ON c.channel_id=o.channel_id
            WHERE o.order_status='paid' AND r.region_name='华东' AND c.channel_type='自营'
              AND o.order_date BETWEEN '2026-02-01' AND '2026-04-30'
            GROUP BY ym ORDER BY ym
        """,
        answer_contains=["大促", "促销"],
        must_mention=["大促"],
        evidence_required=["量化涨幅", "定位到具体日期"],
        note="考能否区分「计划内促销」和「异常」")

    add("g_attribution", "attribution",
        "2026 年 10 月华东复购率降了，是不是老客在流失？", "hard", "S16", 2,
        memory_types=["episodic", "procedural", "semantic"],
        gold_sql="""
            SELECT strftime('%Y-%m', o.order_date) AS ym,
                   COUNT(DISTINCT o.customer_id) AS buyers,
                   COUNT(DISTINCT CASE WHEN c.first_order_date=o.order_date
                                       THEN o.customer_id END) AS new_buyers,
                   ROUND(100.0*COUNT(DISTINCT CASE WHEN c.first_order_date=o.order_date
                                       THEN o.customer_id END)/COUNT(DISTINCT o.customer_id),2) AS new_pct
            FROM fact_order o
            JOIN dim_region r ON r.region_id=o.region_id
            JOIN dim_customer c ON c.customer_id=o.customer_id
            WHERE o.order_status='paid' AND r.region_name='华东'
              AND o.order_date BETWEEN '2026-07-01' AND '2026-10-15'
            GROUP BY ym ORDER BY ym
        """,
        answer_contains=["不是", "新客占比", "结构性"],
        must_mention=["新客占比", "不是老客流失"],
        answer_forbidden=["是老客流失", "老客在流失"],
        evidence_required=["新客占比量化", "老客复购率稳定", "拆分新老客"],
        note="最关键的一题 —— 正确答案是「不是流失」，答成「流失」说明没有情节记忆")

    add("g_attribution", "attribution",
        "2026 年 4 月开始抖音渠道的 GMV 为什么涨了？", "medium", "S16", 3,
        memory_types=["episodic"],
        gold_sql="""
            SELECT strftime('%Y-%m', o.order_date) AS ym, ROUND(SUM(o.pay_amount),2) AS gmv
            FROM fact_order o JOIN dim_channel c ON c.channel_id=o.channel_id
            WHERE o.order_status='paid' AND c.channel_name='抖音旗舰店'
              AND o.order_date BETWEEN '2026-02-01' AND '2026-05-31'
            GROUP BY ym ORDER BY ym
        """,
        answer_contains=["主播", "带货"],
        must_mention=["主播"],
        evidence_required=["量化涨幅", "区分结构性与脉冲"])

    # =====================================================================
    # H. 长程记忆
    # =====================================================================
    add("h_long_horizon", "memory",
        "（这是第 50 次会话）复购率的口径现在还是你上次说的那样吗？", "hard", "S50", 1,
        memory_types=["semantic"],
        ambiguity="跨 50 个 session 后是否还记得口径已变更",
        answer_contains=["已变更", "剔除退款", "2026-09"],
        must_mention=["剔除退款", "2026-09"],
        note="长程题：考检索在记忆条目变多后是否还能命中")

    add("h_long_horizon", "memory",
        "6 月那个 GMV 对不上账的问题后来修了吗？", "medium", "S50", 2,
        memory_types=["episodic"],
        answer_contains=["已修复", "2026-07-05", "历史数据未回刷"],
        must_mention=["已修复"],
        note="考是否记得「已修复但历史未回刷」这个关键细节")

    add("h_long_horizon", "memory",
        "华东 10 月新客占比那事儿你还记得吗？", "medium", "S50", 3,
        memory_types=["episodic"],
        answer_contains=["24.7%", "新客占比", "双十一预热"],
        must_mention=["新客占比"],
        note="考具体数值的记忆保留")

    add("h_long_horizon", "memory",
        "我们团队做指标异动归因的标准流程是什么？", "medium", "S50", 4,
        memory_types=["procedural"],
        answer_contains=["下钻", "假设", "验证"],
        must_mention=["下钻", "假设"],
        note="考程序记忆的长期保留")

    # =====================================================================
    # 为记忆类题目标注「期望命中的记忆条目」
    # ---------------------------------------------------------------------
    # 这是把"记忆模块有没有用"变成可量化指标的关键：
    #   检索命中率 = 命中期望条数 / 期望条数（Recall 口径）
    # 期望条目由出题人事先指定，agent 必须真的检索到才算分。
    #
    # qid 规则：每类题目按定义顺序编号（C-001 起 / D-001 起 ...），稳定可复现。
    # =====================================================================
    MEMORY_TARGETS = {
        # ---- C. 口径题 → 语义记忆 ----
        "C-001": ["sem-repurchase-v2", "pr-repurchase-v2-rule"],
        "C-002": ["sem-aov-v1"],
        "C-003": ["sem-gmv-gross-v1", "sem-gmv-net-v1"],
        "C-004": ["sem-newbuyer-v1"],
        "C-005": ["sem-refund-rate-v1"],
        "C-006": ["sem-sessions-unverified"],
        # ---- D. 复用题 → 情节记忆 ----
        "D-001": ["ep-2026-10-newbuyer"],
        "D-002": ["ep-2026-08-subsidy"],
        "D-003": ["ep-2026-09-logistics"],
        "D-004": ["ep-2026-06-dataquality", "sem-cancel-trap-v1"],
        "D-005": ["ep-2026-03-promo"],
        "D-006": ["ep-2026-04-douyin"],
        # ---- E. 套路题 → 程序记忆 ----
        "E-001": ["pr-attribution-playbook", "pr-region-drilldown"],
        "E-002": ["pr-attribution-playbook", "pr-repurchase-v2-rule", "ep-2026-10-newbuyer"],
        "E-003": ["pr-channel-drilldown"],
        "E-004": ["pr-repurchase-v2-rule", "sem-repurchase-v2"],
        # ---- F. 冲突题 → 语义记忆（新旧版本都要命中）----
        "F-001": ["sem-repurchase-v1", "sem-repurchase-v2"],
        "F-002": ["sem-repurchase-v1"],
        "F-003": ["pr-repurchase-v2-rule"],
        "F-004": ["sem-aov-v1"],
        # ---- G. 归因题 → 情节记忆 ----
        "G-001": ["ep-2026-09-logistics"],
        "G-002": ["ep-2026-08-subsidy"],
        "G-003": ["ep-2026-06-dataquality", "sem-cancel-trap-v1"],
        "G-004": ["ep-2026-03-promo"],
        "G-005": ["ep-2026-10-newbuyer"],
        "G-006": ["ep-2026-04-douyin"],
        # ---- H. 长程题 → 三类都要 ----
        "H-001": ["sem-repurchase-v2"],
        "H-002": ["ep-2026-06-dataquality"],
        "H-003": ["ep-2026-10-newbuyer"],
        "H-004": ["pr-attribution-playbook"],
    }
    for q in Q:
        ids = MEMORY_TARGETS.get(q["qid"])
        if ids:
            q["expected"]["memory_ids"] = ids

    # 校验：所有声明了 requires_memory 的题目都必须有期望记忆
    missing = [q["qid"] for q in Q
               if q["requires_memory"] and "memory_ids" not in q["expected"]]
    if missing:
        raise SystemExit(f"以下题目声明了 requires_memory 但缺少 memory_ids 标注: {missing}")
    # 校验：期望记忆 ID 必须真实存在于 memory_fixtures.jsonl
    known = set()
    fx = os.path.join(HERE, "memory_fixtures.jsonl")
    if os.path.exists(fx):
        with open(fx, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    known.add(json.loads(line)["memory_id"])
    bad = [i for ids in MEMORY_TARGETS.values() for i in ids if i not in known]
    if bad:
        raise SystemExit(f"memory_ids 指向不存在的记忆条目: {sorted(set(bad))}")

    return Q


# --------------------------------------------------------------------------
# 自校验
# --------------------------------------------------------------------------

def check(con, questions):
    bad = []
    for q in questions:
        exp = q["expected"]
        if "reference_sql" in exp:
            try:
                cols, rows = run_sql(con, exp["reference_sql"])
            except Exception as e:
                bad.append((q["qid"], f"SQL 执行失败: {e}"))
                continue
            if not rows:
                bad.append((q["qid"], "gold SQL 返回空结果"))
            if not exp.get("answer_contains") and q["question_type"] == "memory":
                bad.append((q["qid"], "memory 题缺少 answer_contains"))
        # 禁止项不能是必需项的「肯定形式」子串，否则正确答案会被误判成违规
        # （真实踩过：must_mention「不是老客流失」包含 forbidden「是老客流失」）
        # 与 verifier.score_answer 共用同一套否定语境判断，保证出题与评分口径一致。
        forbidden = exp.get("answer_forbidden") or []
        required = (exp.get("answer_contains") or []) + (exp.get("must_mention") or [])
        for fk in forbidden:
            for rk in required:
                if has_affirmative(rk, fk):
                    bad.append((q["qid"], f"禁止项 {fk!r} 是必需项 {rk!r} 的肯定形式子串，会误判正确答案"))
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(DB):
        sys.exit(f"找不到数据库 {DB}\n请先运行: python data/warehouse/seed.py")

    os.makedirs(OUT, exist_ok=True)
    con = sqlite3.connect(DB)
    questions = build(con)

    by_cat = {}
    for q in questions:
        by_cat.setdefault(q["category"], []).append(q)

    total = 0
    for cat, items in by_cat.items():
        path = os.path.join(OUT, f"{cat}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for q in items:
                f.write(json.dumps(q, ensure_ascii=False) + "\n")
        total += len(items)
        print(f"  {path:<46} {len(items):>3} 题   {CATEGORIES[cat]}")

    print(f"\n共 {total} 题，已写入 {OUT}/")

    if args.check:
        print("\n自校验中...")
        bad = check(con, questions)
        if bad:
            for qid, why in bad:
                print(f"  [FAIL] {qid}: {why}")
            sys.exit(1)
        print("  全部通过：gold SQL 可执行、结果非空、memory 题有 key facts")

    con.close()


if __name__ == "__main__":
    main()
