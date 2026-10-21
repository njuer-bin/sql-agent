"""
已知业务效应（KNOWN EFFECTS）
============================================================================
这些是**故意埋进数据里**的真实业务事件。它们的作用是：
  让「归因题」有客观、可验证的答案 —— agent 必须真的查到这些数据才能答对，
  而不是靠编。

评测集里的 G 类（归因推理）题目全部基于这些效应出题。
============================================================================
"""

KNOWN_EFFECTS = [
    {
        "effect_id": "E1",
        "name": "2026 春季大促脉冲",
        "window": ("2026-03-15", "2026-03-20"),
        "scope": {"region_name": "华东", "channel_type": "自营"},
        "mechanism": "campaign C2026_SPRING 大促，补贴 120 万",
        "impact": "华东自营渠道 GMV 提升约 3 倍（大促周 vs 前一周）",
        "expected_direction": "up",
        "primary_metric": "gmv_gross",
        "verification_sql": """
            SELECT strftime('%Y-%m', o.order_date) AS ym,
                   SUM(o.pay_amount) AS gmv
            FROM fact_order o
            JOIN dim_region  r ON r.region_id  = o.region_id
            JOIN dim_channel c ON c.channel_id = o.channel_id
            WHERE o.order_status = 'paid'
              AND r.region_name = '华东'
              AND c.channel_type = '自营'
            GROUP BY ym ORDER BY ym
        """,
    },
    {
        "effect_id": "E2",
        "name": "华东第三方渠道补贴退坡（天猫+京东）",
        "window": ("2026-08-01", None),
        "scope": {"region_name": "华东", "channel_names": ["天猫旗舰店", "京东自营"]},
        "mechanism": "campaign C2026_EAST_3P 于 2026-07-31 到期，补贴归零",
        "impact": "华东（天猫+京东）渠道客单价下降约 22%（补贴退坡导致实付金额下降，订单量不变）",
        "expected_direction": "down",
        "primary_metric": "aov",
        "note": "不含抖音旗舰店 —— 抖音同期受 E6 影响在增长，会掩盖本效应。"
                "本效应体现在**客单价**上，校验时需按单均值比较，不能用总 GMV（会被订单量波动掩盖）",
        "verification_sql": """
            SELECT strftime('%Y-%m', o.order_date) AS ym,
                   SUM(o.pay_amount) AS gmv
            FROM fact_order o
            JOIN dim_region  r ON r.region_id  = o.region_id
            JOIN dim_channel c ON c.channel_id = o.channel_id
            WHERE o.order_status = 'paid'
              AND r.region_name = '华东'
              AND c.channel_name IN ('天猫旗舰店', '京东自营')
            GROUP BY ym ORDER BY ym
        """,
    },
    {
        "effect_id": "E3",
        "name": "华东区 Q4 新客占比上升导致复购率结构性回落",
        "window": ("2026-10-01", None),
        "scope": {"region_name": "华东"},
        "mechanism": "双十一预热期拉新活动，新客占比从 18% 升至 41%",
        "impact": "复购率从约 34% 降至约 26%，但**并非老客流失**",
        "expected_direction": "down",
        "primary_metric": "repurchase_rate",
        "is_structural": True,   # 关键：这是结构性变化，不是流失
        "verification_sql": """
            SELECT strftime('%Y-%m', o.order_date) AS ym,
                   COUNT(DISTINCT o.customer_id) AS buyers,
                   COUNT(DISTINCT CASE WHEN c.first_order_date = o.order_date
                                       THEN o.customer_id END) AS new_buyers
            FROM fact_order o
            JOIN dim_region    r ON r.region_id    = o.region_id
            JOIN dim_customer  c ON c.customer_id  = o.customer_id
            WHERE o.order_status = 'paid'
              AND r.region_name = '华东'
            GROUP BY ym ORDER BY ym
        """,
    },
    {
        "effect_id": "E4",
        "name": "2026-06 已取消订单金额未清零（数据质量陷阱）",
        "window": ("2026-06-01", "2026-06-30"),
        "scope": {"global": True},
        "mechanism": "支付回调重试逻辑缺陷，6 月所有已取消订单的 pay_amount 未清零",
        "impact": "若不过滤 order_status='cancelled'，6 月 GMV 被高估约 3%",
        "expected_direction": "neutral",
        "primary_metric": "gmv_gross",
        "is_data_quality_trap": True,
        "verification_sql": """
            SELECT order_status,
                   COUNT(*) AS cnt,
                   SUM(pay_amount) AS total_pay,
                   ROUND(100.0 * SUM(pay_amount) / (
                       SELECT SUM(pay_amount) FROM fact_order
                       WHERE order_date BETWEEN '2026-06-01' AND '2026-06-30'
                   ), 2) AS pct
            FROM fact_order
            WHERE order_date BETWEEN '2026-06-01' AND '2026-06-30'
            GROUP BY order_status
        """,
    },
    {
        "effect_id": "E5",
        "name": "华北区物流故障",
        "window": ("2026-09-10", "2026-09-14"),
        "scope": {"region_name": "华北"},
        "mechanism": "华北仓 WMS 升级事故，发货延迟 3-5 天",
        "impact": "退款率上升约 5pp，客服工单量上升约 300%",
        "expected_direction": "down",
        "primary_metric": "refund_rate",
        "secondary_metrics": ["ticket_count"],
        "verification_sql": """
            SELECT date(o.order_date) AS d,
                   ROUND(100.0 * SUM(o.refund_amount) / SUM(o.pay_amount), 2) AS refund_rate,
                   COUNT(*) AS orders
            FROM fact_order o
            JOIN dim_region r ON r.region_id = o.region_id
            WHERE o.order_status = 'paid'
              AND r.region_name = '华北'
              AND o.order_date BETWEEN '2026-09-01' AND '2026-09-30'
            GROUP BY d ORDER BY d
        """,
    },
    {
        "effect_id": "E6",
        "name": "抖音渠道爆发增长",
        "window": ("2026-04-01", None),
        "scope": {"channel_name": "抖音旗舰店"},
        "mechanism": "头部主播带货合作启动",
        "impact": "抖音渠道 GMV 提升约 250%",
        "expected_direction": "up",
        "primary_metric": "gmv_gross",
        "verification_sql": """
            SELECT strftime('%Y-%m', o.order_date) AS ym,
                   SUM(o.pay_amount) AS gmv
            FROM fact_order o
            JOIN dim_channel c ON c.channel_id = o.channel_id
            WHERE o.order_status = 'paid'
              AND c.channel_name = '抖音旗舰店'
            GROUP BY ym ORDER BY ym
        """,
    },
]

# 效应索引，供 seed.py 和 build_questions.py 使用
EFFECTS_BY_ID = {e["effect_id"]: e for e in KNOWN_EFFECTS}
