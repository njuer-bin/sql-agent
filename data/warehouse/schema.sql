-- ============================================================================
-- Mini 数仓：模拟一个电商业务的 9 张表
-- 用途：为「多智能体经营分析助手」提供可验证的 SQL 执行环境
-- 生成：python data/warehouse/seed.py
--
-- 设计原则：
--   1. 表结构贴近真实业务（维表 + 事实表分离），让 schema_lookup / 血缘追踪有意义
--   2. 埋入 6 个已知业务效应（见 known_effects.py），让归因题有客观答案
--   3. 埋入 1 个数据质量陷阱，让「不仔细审题」的 agent 必然答错
-- ============================================================================

PRAGMA foreign_keys = OFF;

-- ---------- 维表 ----------

CREATE TABLE dim_date (
    date_id      INTEGER PRIMARY KEY,   -- YYYYMMDD
    dt           DATE    NOT NULL,
    year        INTEGER NOT NULL,
    quarter     INTEGER NOT NULL,
    month       INTEGER NOT NULL,
    day         INTEGER NOT NULL,
    week_of_year INTEGER NOT NULL,
    is_weekend  INTEGER NOT NULL,
    is_holiday  INTEGER NOT NULL,
    holiday_name TEXT,
    is_promo_day INTEGER NOT NULL DEFAULT 0,
    promo_name   TEXT
);

CREATE TABLE dim_region (
    region_id   INTEGER PRIMARY KEY,
    region_name TEXT NOT NULL,          -- 华东 / 华北 / ...
    province    TEXT NOT NULL,
    city        TEXT NOT NULL,
    city_tier   INTEGER NOT NULL        -- 1-5 线
);

CREATE TABLE dim_channel (
    channel_id   INTEGER PRIMARY KEY,
    channel_name TEXT NOT NULL,
    channel_type TEXT NOT NULL          -- 自营 / 第三方 / 线下
);

CREATE TABLE dim_customer (
    customer_id      INTEGER PRIMARY KEY,
    register_date    DATE NOT NULL,
    first_order_date DATE,
    gender           TEXT,
    age_group        TEXT,               -- 18-24 / 25-34 / 35-44 / 45+
    region_id        INTEGER NOT NULL REFERENCES dim_region(region_id),
    acquisition_channel_id INTEGER NOT NULL REFERENCES dim_channel(channel_id)
);

CREATE TABLE dim_product (
    product_id   INTEGER PRIMARY KEY,
    product_name TEXT NOT NULL,
    category_l1  TEXT NOT NULL,          -- 一级类目
    category_l2  TEXT NOT NULL,          -- 二级类目
    brand        TEXT NOT NULL,
    list_price   NUMERIC(12,2) NOT NULL
);

-- ---------- 事实表 ----------

-- 订单事实表
--   order_status : 'paid' | 'cancelled'
--   pay_amount   : 实付金额
--   refund_amount: 退款金额（<= pay_amount，未退款为 0）
--
--   口径陷阱：cancelled 订单正常情况下 pay_amount = 0，
--   但 2026-06 有一批数据异常（pay_amount > 0），
--   不剔除 cancelled 会高估 GMV —— 见 known_effects.E4
CREATE TABLE fact_order (
    order_id      INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES dim_customer(customer_id),
    product_id    INTEGER NOT NULL REFERENCES dim_product(product_id),
    region_id     INTEGER NOT NULL REFERENCES dim_region(region_id),
    channel_id    INTEGER NOT NULL REFERENCES dim_channel(channel_id),
    order_date    DATE    NOT NULL,
    order_status  TEXT    NOT NULL,
    quantity      INTEGER NOT NULL,
    pay_amount    NUMERIC(12,2) NOT NULL,
    refund_amount NUMERIC(12,2) NOT NULL DEFAULT 0
);

CREATE INDEX idx_order_date    ON fact_order(order_date);
CREATE INDEX idx_order_region  ON fact_order(region_id);
CREATE INDEX idx_order_channel ON fact_order(channel_id);
CREATE INDEX idx_order_status  ON fact_order(order_status);

-- 营销活动表
CREATE TABLE fact_marketing_campaign (
    campaign_id    INTEGER PRIMARY KEY,
    campaign_name  TEXT NOT NULL,
    region_id      INTEGER REFERENCES dim_region(region_id),
    channel_id     INTEGER REFERENCES dim_channel(channel_id),
    start_date     DATE NOT NULL,
    end_date       DATE NOT NULL,
    budget         NUMERIC(14,2) NOT NULL,
    subsidy_amount NUMERIC(14,2) NOT NULL
);

-- 流量表（会话维度）
CREATE TABLE fact_web_traffic (
    stat_date  DATE    NOT NULL,
    channel_id INTEGER NOT NULL REFERENCES dim_channel(channel_id),
    region_id  INTEGER NOT NULL REFERENCES dim_region(region_id),
    sessions   INTEGER NOT NULL,
    pageviews  INTEGER NOT NULL,
    PRIMARY KEY (stat_date, channel_id, region_id)
);

-- 客服工单表
CREATE TABLE fact_service_ticket (
    ticket_id     INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES dim_customer(customer_id),
    order_id      INTEGER REFERENCES fact_order(order_id),
    created_date  DATE NOT NULL,
    category      TEXT NOT NULL,          -- 物流 / 质量 / 退款 / 咨询 / 其他
    is_resolved   INTEGER NOT NULL,
    resolve_hours NUMERIC(8,2)
);

CREATE INDEX idx_ticket_date ON fact_service_ticket(created_date);
