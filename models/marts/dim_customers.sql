-- description: Customer dimension with lifetime value and activity
-- materialize: table
select c.*,
       coalesce(f.orders, 0) as lifetime_orders,
       coalesce(f.revenue, 0) as lifetime_revenue,
       f.first_order_date, f.last_order_date,
       case when f.orders is null then 'never_purchased'
            when f.orders = 1 then 'one_time' else 'repeat' end as segment
from {{ ref('stg_customers') }} c
left join (select customer_id, count(*) orders, sum(net_revenue) revenue,
                  min(order_date) first_order_date, max(order_date) last_order_date
           from {{ ref('fct_orders') }} where status = 'completed' group by 1) f using (customer_id)
