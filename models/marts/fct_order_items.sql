-- description: Line-level facts with revenue, cost and margin
-- materialize: table
select i.item_id, i.order_id, o.customer_id, o.order_date, o.status, i.product_id, p.category,
       i.quantity,
       round(i.quantity * p.unit_price * (1 - o.discount_pct / 100.0), 2) as net_revenue,
       round(i.quantity * p.unit_cost, 2) as cost
from {{ ref('stg_order_items') }} i
join {{ ref('stg_orders') }} o using (order_id)
join {{ ref('stg_products') }} p using (product_id)
