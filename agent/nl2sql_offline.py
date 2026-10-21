"""离线确定性 NL2SQL（single-shot 的 fallback 通道）。

定位（对应 PLAN.md 风险表「LLM API 不稳定 → agent 侧做 fallback」）：
  - LLM 未配置 / 调用失败时，从自然语言解析 时间×指标×维度 拼一条合法 SQL
  - 覆盖的是数据类问题（a_basic / b_join 的常见问法）；覆盖不到 → 返回 None，
    agent 明确说"不会"，禁止瞎猜
  - 这不是 LLM 的替代品：接了 SQLAGENT_LLM_* 环境变量的部署会优先走 LLM
"""

from __future__ import annotations

import re

YEAR = "2026"

_DAYS_IN_MONTH = {
    1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
    7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
}


def parse_time(q: str) -> tuple[str, str] | None:
    """从问题中抠时间范围，返回 (start, end)。识别不了返回 None。"""
    qs = q.replace(" ", "")

    # X月X日 到 X月X日
    m = re.search(
        rf"{YEAR}年?(\d{{1,2}})月(\d{{1,2}})[日号]?(?:到|至|~|-)(?:{YEAR}年?)?(\d{{1,2}})月?(\d{{1,2}})[日号]?",
        qs,
    )
    if m:
        a, b, c, d = map(int, m.groups())
        return (f"{YEAR}-{a:02d}-{b:02d}", f"{YEAR}-{c if c else a:02d}-{d:02d}")

    # 季度（中/英）
    m = re.search(r"(第[一二三四]季度|Q[1-4])", qs, re.IGNORECASE)
    if m:
        if "一" in m.group(1) or m.group(1).upper() == "Q1":
            return (f"{YEAR}-01-01", f"{YEAR}-03-31")
        if "二" in m.group(1) or m.group(1).upper() == "Q2":
            return (f"{YEAR}-04-01", f"{YEAR}-06-30")
        if "三" in m.group(1) or m.group(1).upper() == "Q3":
            return (f"{YEAR}-07-01", f"{YEAR}-09-30")
        return (f"{YEAR}-10-01", f"{YEAR}-12-31")

    # 年份 + 月份区间（"7-9 月"）
    m = re.search(rf"{YEAR}年?(\d{{1,2}})(?:-|~)(\d{{1,2}})月", qs)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (f"{YEAR}-{a:02d}-01", f"{YEAR}-{b:02d}-{_DAYS_IN_MONTH[b]}")

    # 年份 + 单月
    m = re.search(rf"{YEAR}年?(\d{{1,2}})月", qs)
    if m:
        a = int(m.group(1))
        return (f"{YEAR}-{a:02d}-01", f"{YEAR}-{a:02d}-{_DAYS_IN_MONTH[a]}")

    # 年（含"至今"）
    if re.search(rf"{YEAR}年", qs) or "今年" in qs:
        return (f"{YEAR}-01-01", f"{YEAR}-12-31")

    # 无年份的月份 —— 默认 shop 的经营年份
    m = re.search(r"(\d{1,2})月", qs)
    if m:
        a = int(m.group(1))
        return (f"{YEAR}-{a:02d}-01", f"{YEAR}-{a:02d}-{_DAYS_IN_MONTH[a]}")

    return None


def _is_paid_metric(q: str) -> bool:
    """大多数指标默认只算 paid 订单。退款率公分母用 paid，工单/流量无该语义。"""
    kw = ["gmv", "订单", "客单价", "新客", "复购", "退款率", "成交", "转化率", "aov"]
    return any(k in q.lower() for k in kw)


def detect_metric(q: str) -> str | None:
    ql = q.lower()
    if "解决时长" in ql:
        return "avg_resolve_hours"
    if "工单" in ql:
        return "ticket_count"
    if "复购率" in ql or "复购" in ql:
        return "repurchase_rate"
    if "退款率" in ql or "退货率" in ql:
        return "refund_rate"
    if "客单价" in ql or "单均价" in ql or "arpu" in ql:
        return "aov"
    if "新客" in ql:
        return "new_buyer_count"
    if "会话" in ql or "流量" in ql or "sessions" in ql:
        return "sessions"
    if ("订单" in ql or re.search(r"下了多少单|多少单|单量|下单量", ql)) and (
        "多少" in ql or "量" in ql or "单数" in ql
    ):
        return "order_count"
    if "gmv" in ql or "成交额" in ql or "交易额" in ql or "做了多少" in ql:
        return "gmv_gross"
    return None


def detect_dimension(q: str) -> str | None:
    ql = q.lower()
    if re.search(r"各月|逐月|趋势|哪个月|月份", ql):
        return "ym"
    if re.search(r"各渠道|每渠道|渠道分别", ql):
        return "channel_name"
    if re.search(r"城市", ql):
        return "city"
    if re.search(r"各区域|各地区|区域分别|区域|地区", ql):
        return "region_name"
    if re.search(r"类目|品类", ql):
        return "category_l1"
    return None


_REGIONS = ["华东", "华南", "华北", "华中", "西南", "东北", "西北"]


def detect_region(q: str) -> str | None:
    for r in _REGIONS:
        if r in q:
            return r
    return None


def detect_limit(q: str) -> int | None:
    """'前三'→3；'最高的 5 个城市'→5；'哪个月最高'→1；'按 xx 降序'（不带 n）→ None。"""
    if "前" in q:
        m = re.search(r"前\s*(\d+)", q)
        if m:
            return int(m.group(1))
    m = re.search(r"(最高|最多|largest)", q)
    if not m:
        return None
    m = re.search(r"(\d+)\s*个", q)
    if m:
        return int(m.group(1))
    m = re.search(r"[一二三四五六七八九十]", q)
    num_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if m and re.search(r"[一两二三]+(个|名|强)", q):
        return num_map.get(m.group(0), 1)
    return 1


def build_offline_sql(q: str) -> str | None:
    """从 NL 问题拼一条 SQL；识别不了返回 None（不允许瞎编）。"""
    metric = detect_metric(q)
    time_range = parse_time(q)
    if metric is None or time_range is None:
        return None

    start, end = time_range
    has_paid = "order_status='paid'" if _is_paid_metric(q) else None
    region = detect_region(q)
    dim = detect_dimension(q)
    limit = detect_limit(q)

    base_where: list[str] = []
    join_facts = "fact_order"
    agg_expr: str | None = None
    needs_dim_join: dict[str, str] = {}

    if metric in ("ticket_count", "avg_resolve_hours"):
        # 工单走 fact_service_ticket
        agg_func = {
            "ticket_count": 0,
            "avg_resolve_hours": 1,
        }[metric]
        where = [f"created_date BETWEEN '{start}' AND '{end}'"]
        if metric == "avg_resolve_hours":
            where.append("is_resolved=1")
            sel = f"ROUND(AVG(resolve_hours),2) AS avg_hours"
        else:
            sel = "COUNT(*) AS tickets"
        sql = f"SELECT {sel} FROM fact_service_ticket WHERE {' AND '.join(where)}"
        if dim == "ym":
            sql = (
                f"SELECT strftime('%Y-%m', created_date) AS ym, ROUND(AVG(resolve_hours),2) AS avg_hours "
                f"FROM fact_service_ticket WHERE {' AND '.join(where)} GROUP BY ym ORDER BY ym"
            )
        return sql

    if has_paid:
        base_where.append("o.order_status='paid'")
    tb = "fact_order"

    if metric == "repurchase_rate":
        return _repurchase_sql(q, start, end, region, dim, limit)

    if metric == "new_buyer_count":
        base_where = [f"c.first_order_date BETWEEN '{start}' AND '{end}'"]
        join = " JOIN dim_region r ON r.region_id=c.region_id" if dim in ("region_name", None) else ""
        if dim == "region_name" or not dim:
            sql = (
                f"SELECT r.region_name, COUNT(DISTINCT c.customer_id) AS new_buyers "
                f"FROM dim_customer c{join} WHERE {' AND '.join(base_where)} "
                f"GROUP BY r.region_name ORDER BY new_buyers DESC"
            )
            return sql
        sql = (
            f"SELECT COUNT(DISTINCT c.customer_id) AS new_buyers FROM dim_customer c "
            f"WHERE {base_where[0]}"
        )
        return sql

    if metric == "sessions":
        sql = (
            f"SELECT ROUND(SUM(sessions),2) AS sessions FROM fact_web_traffic "
            f"WHERE stat_date BETWEEN '{start}' AND '{end}'"
        )
        if dim == "channel_name":
            sql = (
                f"SELECT c.channel_name, SUM(s.sessions) AS sessions FROM fact_web_traffic s "
                f"JOIN dim_channel c ON c.channel_id=s.channel_id "
                f"WHERE s.stat_date BETWEEN '{start}' AND '{end}' GROUP BY c.channel_name "
                f"ORDER BY sessions DESC"
            )
        return sql

    # 以下都是事实表中的订单指标（agg 表达式带表别名前缀，供分组/非分组两种 SELECT 复用）
    if metric == "gmv_gross":
        agg_alias = "gmv"
        agg_base = lambda p: f"ROUND(SUM({p}pay_amount),2)"  # noqa: E731
    elif metric == "order_count":
        agg_alias = "order_cnt"
        agg_base = lambda p: f"COUNT(*)"  # noqa: E731
    elif metric == "refund_rate":
        agg_alias = "refund_rate"
        agg_base = lambda p: f"ROUND(100.0*SUM({p}refund_amount)/SUM({p}pay_amount),2)"  # noqa: E731
    elif metric == "aov":
        agg_alias = "aov"
        agg_base = lambda p: f"ROUND(SUM({p}pay_amount)/COUNT(*),2)"  # noqa: E731
    else:
        return None
    agg_expr = f"{agg_base('')} AS {agg_alias}"

    time_col = "order_date"
    if dim == "ym":
        # "哪个月的 GMV 最高" → 按值排序取 1；"各月 GMV 趋势" → 按时间排序
        ym_end = end[:7]
        is_max = re.search(r"(最高|最多|峰值)", q) is not None and limit == 1
        order = ("gmv DESC LIMIT 1" if is_max else "ym DESC LIMIT 1") if limit == 1 else "ym"
        sql = (
            f"SELECT strftime('%Y-%m', order_date) AS ym, ROUND(SUM(pay_amount),2) AS gmv FROM {tb} "
            f"WHERE order_status='paid' AND order_date >= '{start}' "
            f"AND strftime('%Y-%m', order_date) <= '{ym_end}' GROUP BY ym ORDER BY {order}"
        )
        return sql

    if metric == "gmv_gross" and dim is None:
        # 日均 GMV：今天是分日期的一个特例
        if "日均" in q:
            a, b = start, end
            sql = (
                f"SELECT ROUND(SUM(pay_amount)/(CAST(julianday('{b}')-julianday('{a}')+1 AS REAL)),2) AS avg_daily_gmv "
                f"FROM {tb} WHERE order_status='paid' AND order_date BETWEEN '{a}' AND '{b}'"
            )
            return sql

    if metric == "gmv_gross" and dim == "category_l1" and "占比" in q:
        sql = (
            f"SELECT p.category_l1, ROUND(SUM(o.pay_amount),2) AS gmv, "
            f"ROUND(100.0*SUM(o.pay_amount)/(SELECT SUM(pay_amount) FROM {tb} "
            f"WHERE order_status='paid' AND order_date BETWEEN '{start}' AND '{end}'),2) AS pct "
            f"FROM {tb} o JOIN dim_product p ON p.product_id=o.product_id "
            f"WHERE o.order_status='paid' AND o.order_date BETWEEN '{start}' AND '{end}' "
            f"GROUP BY p.category_l1 ORDER BY gmv DESC"
        )
        return sql

    join_sql = ""
    if dim == "channel_name" or (dim is not None and region):
        join_sql += " JOIN dim_channel c ON c.channel_id=o.channel_id"
    if dim in ("region_name", "city") or region:
        join_sql += " JOIN dim_region r ON r.region_id=o.region_id"
    if dim == "category_l1":
        join_sql += " JOIN dim_product p ON p.product_id=o.product_id"

    where = list(base_where) + [f"o.{time_col} BETWEEN '{start}' AND '{end}'"]
    if region:
        where.append(f"r.region_name='{region}'")

    group_expr, alias = {
        "channel_name": ("c.channel_name", "channel_name"),
        "region_name": ("r.region_name", "region_name"),
        "city": ("r.city", "city"),
        "category_l1": ("p.category_l1", "category_l1"),
        None: (None, None),
    }.get(dim, (None, None))

    if group_expr:
        limit_clause = f" LIMIT {limit}" if limit else ""
        sql = (
            f"SELECT {group_expr}, {agg_base('o.')} AS {agg_alias} FROM {tb} o{join_sql} "
            f"WHERE {' AND '.join(where)} GROUP BY {group_expr} "
            f"ORDER BY {agg_alias} DESC{limit_clause}"
        )
        return sql

    sql = (
        f"SELECT {agg_expr} FROM {tb} o{join_sql} WHERE {' AND '.join(where)}"
    )
    return sql


def _repurchase_sql(q: str, start: str, end: str, region: str | None, dim: str | None, limit: int | None) -> str:
    """复购率：默认口径 = 当期下 ≥2 单的购买用户占比（金具口径 v1），退款用户剔除（v2）。"""
    per_user_where = f"o.order_status='paid' AND o.order_date BETWEEN '{start}' AND '{end}'"
    if region:
        # 复购率按维度拆分 —— 现阶段只支持区域
        pass
    if dim == "region_name":
        sql = (
            f"WITH per_user AS (SELECT o.region_id, o.customer_id, COUNT(*) AS n, "
            f"SUM(o.refund_amount) AS ref FROM fact_order o WHERE {per_user_where} "
            f"GROUP BY o.region_id, o.customer_id), "
            f"agg AS (SELECT region_id, 100.0*COUNT(CASE WHEN n>=2 THEN 1 END)/COUNT(*) AS rep_rate "
            f"FROM per_user WHERE ref=0 GROUP BY region_id) "
            f"SELECT r.region_name, ROUND(a.rep_rate,2) AS repurchase_rate FROM agg a "
            f"JOIN dim_region r ON r.region_id=a.region_id ORDER BY a.rep_rate DESC"
        )
        if limit:
            sql += f" LIMIT {limit}"
        return sql
    sql = (
        f"WITH per_user AS (SELECT o.customer_id, COUNT(*) AS n, SUM(o.refund_amount) AS ref "
        f"FROM fact_order o WHERE {per_user_where} GROUP BY o.customer_id) "
        f"SELECT ROUND(100.0*COUNT(CASE WHEN n>=2 THEN 1 END)/COUNT(*),2) AS repurchase_rate "
        f"FROM per_user WHERE ref=0"
    )
    return sql
