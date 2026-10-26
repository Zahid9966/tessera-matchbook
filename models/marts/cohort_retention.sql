-- description: Monthly signup cohorts and the share still ordering N months later
-- materialize: table
with cohorts as (select customer_id, date_trunc('month', signup_date) cohort_month from {{ ref('dim_customers') }}),
sizes as (select cohort_month, count(*) size from cohorts group by 1),
act as (select distinct c.cohort_month, o.customer_id,
               date_diff('month', c.cohort_month, date_trunc('month', o.order_date)) month_n
        from {{ ref('fct_orders') }} o join cohorts c using (customer_id) where o.status = 'completed')
select a.cohort_month, a.month_n, count(*) active_customers, s.size as cohort_size,
       round(count(*) * 1.0 / s.size, 4) as retention
from act a join sizes s using (cohort_month) where a.month_n >= 0 group by a.cohort_month, a.month_n, s.size order by 1, 2
