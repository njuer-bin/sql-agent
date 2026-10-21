#!/usr/bin/env python
"""量化校验 6 个已知效应是否真的出现在数据里。"""
import json
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "warehouse.db")
con = sqlite3.connect(DB)
def q(sql, params=()):
    return con.execute(sql, params).fetchall()


def gmv(where=""):
    sql = """
        SELECT COALESCE(SUM(o.pay_amount),0) FROM fact_order o
        JOIN dim_region r ON r.region_id = o.region_id
        JOIN dim_channel c ON c.channel_id = o.channel_id
        WHERE o.order_status = 'paid' {w}
    """.format(w=where)
    return q(sql)[0][0]


print("=" * 78)
print("E1  春季大促脉冲（华东自营，2026-03-15~20 vs 前一周）")
print("=" * 78)
a = gmv("AND r.region_name='华东' AND c.channel_type='自营' AND o.order_date BETWEEN '2026-03-15' AND '2026-03-20'")
b = gmv("AND r.region_name='华东' AND c.channel_type='自营' AND o.order_date BETWEEN '2026-03-08' AND '2026-03-14'")
print(f"  大促周 GMV = {a:,.0f}   前一周 = {b:,.0f}   倍数 = {a/b:.2f}x   "
      f"{'PASS' if 1.5 < a/b < 3.5 else 'FAIL'}")

print()
print("=" * 78)
print("E2  华东第三方补贴退坡（天猫+京东，客单价 2026-09 vs 2026-07）")
print("=" * 78)
def aov(where=""):
    sql = """
        SELECT COALESCE(AVG(o.pay_amount),0) FROM fact_order o
        JOIN dim_region r ON r.region_id = o.region_id
        JOIN dim_channel c ON c.channel_id = o.channel_id
        WHERE o.order_status = 'paid' {w}
    """.format(w=where)
    return q(sql)[0][0]
a = aov("AND r.region_name='华东' AND c.channel_name IN ('天猫旗舰店','京东自营') AND o.order_date BETWEEN '2026-08-01' AND '2026-10-15'")
b = aov("AND r.region_name='华东' AND c.channel_name IN ('天猫旗舰店','京东自营') AND o.order_date BETWEEN '2026-07-01' AND '2026-07-31'")
print(f"  退坡后客单价 = {a:,.1f}   7月客单价 = {b:,.1f}   比值 = {a/b:.3f}   "
      f"（预期 ≈0.78）  {'PASS' if 0.70 < a/b < 0.86 else 'FAIL'}")

print()
print("=" * 78)
print("E3  华东 Q4 新客占比上升")
print("=" * 78)
rows = q("""
    SELECT strftime('%Y-%m', o.order_date) ym,
           COUNT(DISTINCT o.customer_id) buyers,
           COUNT(DISTINCT CASE WHEN c.first_order_date = o.order_date
                               THEN o.customer_id END) newb
    FROM fact_order o
    JOIN dim_region r ON r.region_id = o.region_id
    JOIN dim_customer c ON c.customer_id = o.customer_id
    WHERE o.order_status='paid' AND r.region_name='华东'
      AND o.order_date BETWEEN '2026-07-01' AND '2026-10-15'
    GROUP BY ym ORDER BY ym
""")
for ym, buyers, newb in rows:
    print(f"  {ym}: 新客占比 = {newb/buyers*100:5.1f}%  (buyers={buyers}, new={newb})")
if rows:
    before = rows[0][2] / rows[0][1]
    after = rows[-1][2] / rows[-1][1]
    print(f"  新客占比 {before*100:.1f}% -> {after*100:.1f}%   {'PASS' if after - before > 0.08 else 'CHECK'}")

print()
print("=" * 78)
print("E4  数据质量陷阱（2026-06 cancelled 但有金额）")
print("=" * 78)
rows = q("""
    SELECT order_status, COUNT(*), SUM(pay_amount) FROM fact_order
    WHERE order_date BETWEEN '2026-06-01' AND '2026-06-30' GROUP BY order_status
""")
tot = sum(r[2] for r in rows)
for st, cnt, amt in rows:
    print(f"  {st:10s} cnt={cnt:6d}  pay_sum={amt:>14,.0f}  占当月 {amt/tot*100:.2f}%")
canc = [r for r in rows if r[0] == 'cancelled']
if canc:
    pct = canc[0][2] / tot * 100
    print(f"  cancelled 金额占比 = {pct:.2f}%   {'PASS' if 2.5 < pct < 6.0 else 'FAIL'}")

print()
print("=" * 78)
print("E5  华北物流故障（退款率 2026-09-10~14 vs 09-01~09）")
print("=" * 78)
a = q("""SELECT SUM(o.refund_amount), SUM(o.pay_amount) FROM fact_order o
         JOIN dim_region r ON r.region_id=o.region_id
         WHERE o.order_status='paid' AND r.region_name='华北'
           AND o.order_date BETWEEN '2026-09-10' AND '2026-09-14'""")[0]
b = q("""SELECT SUM(o.refund_amount), SUM(o.pay_amount) FROM fact_order o
         JOIN dim_region r ON r.region_id=o.region_id
         WHERE o.order_status='paid' AND r.region_name='华北'
           AND o.order_date BETWEEN '2026-09-01' AND '2026-09-09'""")[0]
ra, rb = a[0] / a[1] * 100, b[0] / b[1] * 100
print(f"  故障期退款率 = {ra:.2f}%   前期 = {rb:.2f}%   差 = {ra-rb:+.2f}pp   "
      f"{'PASS' if ra - rb > 3 else 'FAIL'}")

print()
print("=" * 78)
print("E6  抖音渠道爆发（2026-04 起 vs 2026-03）")
print("=" * 78)
a = gmv("AND c.channel_name='抖音旗舰店' AND o.order_date BETWEEN '2026-04-01' AND '2026-04-30'")
b = gmv("AND c.channel_name='抖音旗舰店' AND o.order_date BETWEEN '2026-03-01' AND '2026-03-31'")
print(f"  4月 GMV = {a:,.0f}   3月 = {b:,.0f}   倍数 = {a/b:.2f}x   "
      f"{'PASS' if a/b > 2.5 else 'FAIL'}")

print()
print("=" * 78)
print("基础分布检查")
print("=" * 78)
r = q("""SELECT COUNT(DISTINCT customer_id), COUNT(*) FROM fact_order WHERE order_status='paid'""")[0]
print(f"  有订单客户数 = {r[0]:,}   paid 订单数 = {r[1]:,}")
rows = q("""
    SELECT COUNT(DISTINCT customer_id) FROM fact_order
    WHERE order_status='paid' AND order_date BETWEEN '2026-09-01' AND '2026-09-30'
""")
buyers = rows[0][0]
multi = q("""
    SELECT COUNT(*) FROM (
        SELECT customer_id FROM fact_order
        WHERE order_status='paid' AND order_date BETWEEN '2026-09-01' AND '2026-09-30'
        GROUP BY customer_id HAVING COUNT(*) >= 2
    )
""")[0][0]
print(f"  2026-09 下单用户 = {buyers:,}   其中下单>=2次 = {multi:,}   复购率 = {multi/buyers*100:.1f}%")
con.close()
