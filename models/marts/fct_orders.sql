-- description: Order grain facts (revenue net of discount)
-- materialize: table
select o.order_id, o.customer_id, o.order_date, o.status, o.payment_method, o.discount_pct,
       count(i.item_id) as line_count, sum(i.quantity) as units,
       coalesce(sum(i.net_revenue), 0) as net_revenue, coalesce(sum(i.cost), 0) as cost
from {{ ref('stg_orders') }} o
left join {{ ref('fct_order_items') }} i using (order_id)
group by all
