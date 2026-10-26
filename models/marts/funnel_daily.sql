-- description: Conversion funnel counts per day and device
-- materialize: table
select event_date, device,
       count(distinct case when event_name='page_view' then customer_id end) as viewers,
       count(distinct case when event_name='add_to_cart' then customer_id end) as carters,
       count(distinct case when event_name='checkout_start' then customer_id end) as checkouts,
       count(distinct case when event_name='purchase' then customer_id end) as purchasers
from {{ ref('stg_events') }} group by 1, 2
