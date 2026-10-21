#!/usr/bin/env python
"""
Mini 数仓生成器
============================================================================
生成一个贴近真实电商业务的 SQLite 数仓，并**故意埋入 6 个已知业务效应**
（见 known_effects.py），使得归因类题目有客观、可验证的答案。

用法：
    python data/warehouse/seed.py                # 生成到 data/warehouse/warehouse.db
    python data/warehouse/seed.py --verify       # 生成后校验 6 个效应是否真的存在

输出：
    data/warehouse/warehouse.db
    data/warehouse/effect_check.json             # 效应自校验结果
============================================================================
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from known_effects import KNOWN_EFFECTS  # noqa: E402

# ---------------------------------------------------------------- 配置 ---

SEED = 20261021
START = date(2025, 1, 1)
END = date(2026, 10, 15)

BASE_DAILY_ORDERS = 180          # 日均订单量（基准）
INITIAL_CUSTOMERS = 3000         # 期初客户池
CUST_GROWTH_PER_DAY = 13.5       # 客户池日均净增

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "warehouse.db")
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# 月度季节因子（1-12 月）
MONTH_FACTOR = {
    1: 1.15, 2: 0.75, 3: 1.05, 4: 1.00, 5: 1.10, 6: 1.20,
    7: 0.90, 8: 0.95, 9: 1.00, 10: 1.15, 11: 1.50, 12: 1.25,
}

WEEKEND_FACTOR = 1.35
HOLIDAY_FACTOR = 1.60
PROMO_FACTOR = 1.60

CANCEL_RATE = 0.04               # 正常取消率
BASE_REFUND_RATE = 0.035         # 正常退款率
TICKET_RATE = 0.015              # 工单率

# ------------------------------------------------------------ 维度定义 ---

REGIONS = [
    (1, "华东", "上海", "上海", 1), (2, "华东", "浙江", "杭州", 1),
    (3, "华东", "江苏", "南京", 2), (4, "华东", "江苏", "苏州", 2),
    (5, "华东", "安徽", "合肥", 3),
    (6, "华北", "北京", "北京", 1), (7, "华北", "天津", "天津", 2),
    (8, "华北", "河北", "石家庄", 3), (9, "华北", "山西", "太原", 3),
    (10, "华南", "广东", "广州", 1), (11, "华南", "广东", "深圳", 1),
    (12, "华南", "广东", "佛山", 2), (13, "华南", "广东", "东莞", 2),
    (14, "华中", "湖北", "武汉", 2), (15, "华中", "湖南", "长沙", 2),
    (16, "华中", "河南", "郑州", 3),
    (17, "西南", "四川", "成都", 2), (18, "西南", "重庆", "重庆", 2),
    (19, "西南", "云南", "昆明", 3),
    (20, "西北", "陕西", "西安", 2), (21, "西北", "甘肃", "兰州", 4),
    (22, "西北", "新疆", "乌鲁木齐", 4),
    (23, "东北", "辽宁", "沈阳", 2), (24, "东北", "辽宁", "大连", 3),
    (25, "东北", "吉林", "长春", 3), (26, "东北", "黑龙江", "哈尔滨", 3),
]

# 渠道名, 类型, 订单占比
CHANNELS = [
    ("自营APP",     "自营",   0.22),
    ("自营小程序",   "自营",   0.13),
    ("天猫旗舰店",   "第三方", 0.20),
    ("京东自营",     "第三方", 0.15),
    ("抖音旗舰店",   "第三方", 0.15),
    ("线下门店",     "线下",   0.15),
]

CATEGORIES = {
    "家用电器": (["冰箱", "洗衣机", "空调", "电视", "厨电"], 5, 2200.0),
    "手机数码": (["手机", "平板", "耳机", "智能手表"], 8, 3800.0),
    "服饰鞋包": (["男装", "女装", "运动鞋", "箱包"], 4, 480.0),
    "美妆个护": (["护肤", "彩妆", "洗护"], 5, 320.0),
    "食品饮料": (["零食", "酒水", "生鲜"], 6, 140.0),
    "母婴用品": (["奶粉", "尿裤", "玩具"], 6, 520.0),
}

BRANDS = ["云澜", "北辰", "初芒", "铸界", "沐光", "栖迟", "衡玖", "未名"]
AGE_GROUPS = ["18-24", "25-34", "35-44", "45+"]
AGE_WEIGHTS = [0.22, 0.38, 0.26, 0.14]

# 大促/节假日（影响 dim_date 和订单量）
PROMO_DAYS = {
    "2026-03-15": "春季大促", "2026-03-16": "春季大促", "2026-03-17": "春季大促",
    "2026-03-18": "春季大促", "2026-03-19": "春季大促", "2026-03-20": "春季大促",
    "2026-06-18": "618 大促",
    "2025-11-11": "双11", "2026-11-11": "双11",
}
HOLIDAYS = {
    "2025-01-28": "春节", "2025-01-29": "春节", "2025-01-30": "春节", "2025-01-31": "春节",
    "2025-02-01": "春节", "2025-02-02": "春节", "2025-02-03": "春节", "2025-02-04": "春节",
    "2025-10-01": "国庆", "2025-10-02": "国庆", "2025-10-03": "国庆",
    "2025-10-04": "国庆", "2025-10-05": "国庆", "2025-10-06": "国庆", "2025-10-07": "国庆",
    "2026-02-16": "春节", "2026-02-17": "春节", "2026-02-18": "春节", "2026-02-19": "春节",
    "2026-02-20": "春节", "2026-02-21": "春节", "2026-02-22": "春节",
    "2026-10-01": "国庆", "2026-10-02": "国庆", "2026-10-03": "国庆",
    "2026-10-04": "国庆", "2026-10-05": "国庆", "2026-10-06": "国庆", "2026-10-07": "国庆",
}


# ------------------------------------------------------------ 辅助函数 ---

def weighted_choice(rng: random.Random, items, weights):
    return rng.choices(items, weights=weights, k=1)[0]


def build_date_dim(rng):
    rows = []
    d = START
    while d <= END:
        iso = d.isocalendar()
        key = d.isoformat()
        is_weekend = 1 if d.weekday() >= 5 else 0
        promo = PROMO_DAYS.get(key)
        holiday = HOLIDAYS.get(key)
        rows.append((
            int(d.strftime("%Y%m%d")), key, d.year, (d.month - 1) // 3 + 1,
            d.month, d.day, iso[1], is_weekend,
            1 if holiday else 0, holiday,
            1 if promo else 0, promo,
        ))
        d += timedelta(days=1)
    return rows


def build_products(rng):
    rows = []
    pid = 1
    for l1, (l2s, brand_n, base_price) in CATEGORIES.items():
        for l2 in l2s:
            for b in range(brand_n):
                brand = BRANDS[(pid + b) % len(BRANDS)]
                price = round(base_price * rng.uniform(0.35, 2.4), 2)
                rows.append((pid, f"{brand}·{l2}·{b+1}号", l1, l2, brand, price))
                pid += 1
    return rows


# ------------------------------------------------------------ 主生成逻辑 ---

def generate():
    rng = random.Random(SEED)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        cur.executescript(f.read())

    # ---- 维表 ----
    cur.executemany(
        "INSERT INTO dim_date VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", build_date_dim(rng))
    cur.executemany("INSERT INTO dim_region VALUES (?,?,?,?,?)", REGIONS)
    cur.executemany(
        "INSERT INTO dim_channel VALUES (?,?,?)",
        [(i + 1, name, ctype) for i, (name, ctype, _) in enumerate(CHANNELS)])
    products = build_products(rng)
    cur.executemany(
        "INSERT INTO dim_product VALUES (?,?,?,?,?,?)", products)

    region_ids = [r[0] for r in REGIONS]
    # 地区订单权重（华东最大）
    region_weights = {
        "华东": 0.28, "华北": 0.18, "华南": 0.20, "华中": 0.12,
        "西南": 0.10, "西北": 0.06, "东北": 0.06,
    }
    region_w = [region_weights[r[1]] for r in REGIONS]
    channel_w = [c[2] for c in CHANNELS]

    # ---- 客户池 ----
    # 每个客户一个活跃度 p_i（对数正态），每天按概率下单 → 自然形成复购分布
    customers = []          # (cid, register_date, region_id, channel_id, activity, gender, age_group)
    next_cid = 1

    def register(n, day):
        nonlocal next_cid
        out = []
        for _ in range(n):
            act = rng.lognormvariate(mu=0.0, sigma=1.0)
            out.append((
                next_cid, day.isoformat(),
                weighted_choice(rng, region_ids, region_w),
                weighted_choice(rng, list(range(1, len(CHANNELS) + 1)), channel_w),
                act,
                rng.choice(["男", "女"]),
                weighted_choice(rng, AGE_GROUPS, AGE_WEIGHTS),
            ))
            next_cid += 1
        return out

    customers.extend(register(INITIAL_CUSTOMERS, START))
    cust_rows = [(c[0], c[1], None, c[5], c[6], c[2], c[3]) for c in customers]
    cur.executemany(
        "INSERT INTO dim_customer VALUES (?,?,?,?,?,?,?)", cust_rows)

    # ---- 订单 ----
    orders = []
    traffic = []
    tickets = []
    next_oid = 1
    next_tid = 1

    product_ids = [p[0] for p in products]
    product_price = {p[0]: p[5] for p in products}
    # 商品热度（幂律，让头部商品有区分度）
    prod_rank = {pid: 1.0 / (i + 1) ** 0.7 for i, pid in enumerate(product_ids)}

    def day_factor(d: date) -> float:
        f = MONTH_FACTOR[d.month]
        if d.isoformat() in PROMO_DAYS:
            f *= PROMO_FACTOR
        elif d.isoformat() in HOLIDAYS:
            f *= HOLIDAY_FACTOR
        elif d.weekday() >= 5:
            f *= WEEKEND_FACTOR
        return f

    d = START
    while d <= END:
        # 客户池自然增长
        growth = max(1, int(round(CUST_GROWTH_PER_DAY * day_factor(d) ** 0.35)))
        new_custs = register(growth, d)
        customers.extend(new_custs)
        cur.executemany(
            "INSERT INTO dim_customer VALUES (?,?,?,?,?,?,?)",
            [(c[0], c[1], None, c[5], c[6], c[2], c[3]) for c in new_custs])

        # 当日订单量
        target = BASE_DAILY_ORDERS * day_factor(d)
        total_act = sum(c[4] for c in customers) or 1.0
        scale = target / total_act

        # E3：华东 Q4 拉新 —— 提高新客占比
        e3_active = (d >= date(2026, 10, 1))
        recent_pool = [c for c in customers if c[1] >= (d - timedelta(days=45)).isoformat()]

        placed = 0
        for c in customers:
            if rng.random() < c[4] * scale:
                placed += 1
        # 用泊松采样校正到目标量（避免日内波动过大）
        placed = max(0, int(rng.gauss(placed, math.sqrt(max(placed, 1)) * 0.5)))

        picked = []
        for _ in range(placed):
            if e3_active and recent_pool and rng.random() < 0.41:
                picked.append(rng.choice(recent_pool))
            else:
                picked.append(rng.choices(customers, weights=[c[4] for c in customers], k=1)[0])

        for c in picked:
            cid, reg_date, cregion, cchan, _act, _g, _a = c
            pid = weighted_choice(rng, product_ids, [prod_rank[p] for p in product_ids])
            qty = 1 if rng.random() < 0.86 else rng.randint(2, 4)
            amount = round(product_price[pid] * qty * rng.uniform(0.72, 1.0), 2)
            status = "cancelled" if rng.random() < CANCEL_RATE else "paid"
            pay = 0.0 if status == "cancelled" else amount
            refund = round(pay * rng.uniform(0.15, 1.0), 2) if (
                status == "paid" and rng.random() < BASE_REFUND_RATE) else 0.0

            # ============ 注入已知效应 ============
            rname = REGIONS[cregion - 1][1]
            cname = CHANNELS[cchan - 1][0]
            ctype = CHANNELS[cchan - 1][1]

            # E1 春季大促：华东自营 GMV ×2.0
            if ("2026-03-15" <= d.isoformat() <= "2026-03-20"
                    and rname == "华东" and ctype == "自营"):
                pay = round(pay * 2.0, 2) if status == "paid" else 0.0

            # E2 华东第三方补贴退坡：天猫+京东 ×0.78（不含抖音，抖音受 E6 影响）
            if (d >= date(2026, 8, 1) and rname == "华东"
                    and cname in ("天猫旗舰店", "京东自营")):
                pay = round(pay * 0.78, 2) if status == "paid" else 0.0

            # E6 抖音渠道爆发：×3.5
            if d >= date(2026, 4, 1) and cname == "抖音旗舰店":
                pay = round(pay * 3.5, 2) if status == "paid" else 0.0

            # E4 数据质量陷阱：6 月所有已取消订单 pay_amount 未清零
            if d.year == 2026 and d.month == 6 and status == "cancelled":
                pay = round(amount, 2)      # ← 陷阱：cancelled 但有金额

            # E5 华北物流故障：退款率升至约 6%
            if (date(2026, 9, 10) <= d <= date(2026, 9, 14)) and rname == "华北":
                if status == "paid":
                    refund = round(pay * 0.06, 2)

            orders.append((
                next_oid, cid, pid, cregion, cchan, d.isoformat(),
                status, qty, pay, refund))
            next_oid += 1

            # 流量（会话数 ≈ 订单的 9 倍）
            # 工单
            if rng.random() < TICKET_RATE:
                cat = weighted_choice(rng, ["物流", "质量", "退款", "咨询", "其他"],
                                      [0.30, 0.22, 0.18, 0.20, 0.10])
                tickets.append((
                    next_tid, cid, next_oid - 1, d.isoformat(), cat,
                    1 if rng.random() < 0.88 else 0,
                    round(rng.uniform(0.5, 72.0), 2)))
                next_tid += 1

        # E5 附加：华北物流故障期间工单量暴涨 300%
        if date(2026, 9, 10) <= d <= date(2026, 9, 14):
            extra = int(placed * TICKET_RATE * 3.0)
            for _ in range(extra):
                hc = [c for c in customers if REGIONS[c[2] - 1][1] == "华北"]
                if not hc:
                    break
                c = rng.choice(hc)
                tickets.append((
                    next_tid, c[0], None, d.isoformat(), "物流",
                    1 if rng.random() < 0.75 else 0, round(rng.uniform(2.0, 96.0), 2)))
                next_tid += 1

        # 流量表
        for ch in range(1, len(CHANNELS) + 1):
            for rg in region_ids:
                base = placed / (len(CHANNELS) * len(region_ids))
                sess = max(0, int(rng.gauss(base * 9, base * 2.2)))
                traffic.append((d.isoformat(), ch, rg, sess, sess * rng.randint(3, 7)))

        d += timedelta(days=1)

        if d.day == 1:
            con.commit()
            print(f"  ... {d}  orders={len(orders)}", flush=True)

    cur.executemany(
        "INSERT INTO fact_order VALUES (?,?,?,?,?,?,?,?,?,?)", orders)
    cur.executemany(
        "INSERT INTO fact_web_traffic VALUES (?,?,?,?,?)", traffic)
    cur.executemany(
        "INSERT INTO fact_service_ticket VALUES (?,?,?,?,?,?,?)", tickets)

    # ---- 营销活动 ----
    campaigns = [
        (1, "2026春季大促", 1, 1, "2026-03-15", "2026-03-20", 1_200_000, 980_000),
        (2, "2026华东第三方补贴", 1, 3, "2025-08-01", "2026-07-31", 2_000_000, 1_750_000),
        (3, "2026双十一预热", None, None, "2026-10-20", "2026-11-11", 5_000_000, 4_200_000),
        (4, "2026抖音主播合作", None, 5, "2026-04-01", "2026-12-31", 1_500_000, 1_100_000),
        (5, "2025双十一", None, None, "2025-10-20", "2025-11-11", 4_500_000, 3_800_000),
    ]
    cur.executemany(
        "INSERT INTO fact_marketing_campaign VALUES (?,?,?,?,?,?,?,?)", campaigns)

    con.commit()

    # 回填 first_order_date
    cur.execute("""
        UPDATE dim_customer
        SET first_order_date = (
            SELECT MIN(o.order_date) FROM fact_order o
            WHERE o.customer_id = dim_customer.customer_id
              AND o.order_status = 'paid'
        )
        WHERE customer_id IN (SELECT customer_id FROM fact_order)
    """)
    con.commit()

    print(f"\n生成完成：orders={len(orders)}  tickets={len(tickets)}  "
          f"traffic={len(traffic)}  customers={len(customers)}")
    con.close()
    return len(orders)


def verify_effects():
    """校验 6 个已知效应是否真的出现在数据里。"""
    con = sqlite3.connect(DB_PATH)
    results = {}
    for e in KNOWN_EFFECTS:
        rows = con.execute(e["verification_sql"]).fetchall()
        results[e["effect_id"]] = {
            "name": e["name"],
            "expected": e["impact"],
            "row_count": len(rows),
            "sample": [list(r) for r in rows[:3]],
            "tail": [list(r) for r in rows[-3:]],
        }
    con.close()
    out = os.path.join(os.path.dirname(DB_PATH), "effect_check.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"效应自校验结果已写入 {out}")
    for k, v in results.items():
        print(f"  {k} {v['name']}: {v['row_count']} 行")
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    if os.path.exists(DB_PATH) and not args.verify:
        os.remove(DB_PATH)
        print(f"已删除旧库 {DB_PATH}")

    if not args.verify:
        generate()
    if args.verify or not args.verify:
        verify_effects()
