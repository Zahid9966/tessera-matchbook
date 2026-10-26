-- description: Product-level sales, margin and return rate
-- materialize: table
select p.product_id, p.name, p.category,
       sum(case when i.status='completed' then i.quantity else 0 end) as units_sold,
       sum(case when i.status='completed' then i.net_revenue else 0 end) as revenue,
       sum(case when i.status='completed' then i.net_revenue - i.cost else 0 end) as gross_profit,
       round(sum(case when i.status='returned' then 1 else 0 end) * 1.0 / nullif(count(*),0), 4) as return_rate
from {{ ref('stg_products') }} p left join {{ ref('fct_order_items') }} i using (product_id) group by all
