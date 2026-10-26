-- description: Products with unit margin
select product_id, name, category, cast(unit_price as decimal(10,2)) as unit_price, cast(unit_cost as decimal(10,2)) as unit_cost,
       round(cast(unit_price as decimal(10,2)) - cast(unit_cost as decimal(10,2)), 2) as unit_margin
from {{ source('products') }}
