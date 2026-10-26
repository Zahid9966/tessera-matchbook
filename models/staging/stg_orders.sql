-- description: Deduplicated orders; at-least-once delivery produced repeats
select order_id, customer_id, cast(order_ts as timestamp) as order_ts,
       cast(order_ts as date) as order_date, status, payment_method, cast(discount_pct as integer) as discount_pct
from (select *, row_number() over (partition by order_id order by order_ts) rn from {{ source('orders') }})
where rn = 1
