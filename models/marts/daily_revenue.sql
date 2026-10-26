-- description: Revenue, orders and AOV per day (completed orders only)
-- materialize: table
select order_date, count(*) as orders, sum(net_revenue) as revenue, sum(net_revenue - cost) as gross_profit,
       round(avg(net_revenue), 2) as aov
from {{ ref('fct_orders') }} where status = 'completed' group by 1 order by 1
