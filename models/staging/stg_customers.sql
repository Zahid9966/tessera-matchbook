-- description: Deduplicated, normalised customers (one row per customer_id)
select customer_id,
       upper(left(split_part(trim(full_name),' ',1),1)) || lower(substr(split_part(trim(full_name),' ',1),2)) || ' ' || upper(left(split_part(trim(full_name),' ',2),1)) || lower(substr(split_part(trim(full_name),' ',2),2)) as full_name,
       nullif(lower(trim(email)), '') as email,
       upper(country) as country,
       cast(signup_date as date) as signup_date,
       acquisition_channel
from (select *, row_number() over (partition by customer_id order by signup_date) rn from {{ source('customers') }})
where rn = 1
