-- description: Acquisition channel quality: customers, conversion, LTV
-- materialize: table
select acquisition_channel, count(*) as customers,
       sum(case when lifetime_orders > 0 then 1 else 0 end) as buyers,
       round(avg(case when lifetime_orders > 0 then 1.0 else 0.0 end), 4) as conversion,
       round(avg(lifetime_revenue), 2) as avg_ltv, round(sum(lifetime_revenue), 2) as total_revenue
from {{ ref('dim_customers') }} group by 1
