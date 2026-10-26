-- description: Clickstream events
select event_id, customer_id, event_name, cast(event_ts as timestamp) as event_ts,
       cast(event_ts as date) as event_date, device
from {{ source('events') }}
