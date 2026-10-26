-- description: Order line items
select item_id, order_id, product_id, cast(quantity as integer) as quantity from {{ source('order_items') }}
