"""Synthetic e-commerce source systems, written as raw CSV/JSONL with realistic mess
(duplicates, nulls, mixed case, late-arriving rows) so the pipeline has work to do."""
from __future__ import annotations
import csv, json, random, datetime as dt
from pathlib import Path
from . import config

FIRST = "Ava Noah Mia Liam Zoe Ethan Ivy Omar Lena Raj Sana Leo Nina Kai Tara Hugo Yara Ben Dia Finn".split()
LAST = "Patel Smith Khan Garcia Chen Okafor Silva Novak Rossi Kim Haddad Müller Brown Singh Lopez".split()
COUNTRIES = [("US", .4), ("GB", .12), ("DE", .1), ("IN", .15), ("BR", .08), ("AU", .05), ("FR", .06), ("JP", .04)]
CATEGORIES = {
    "Electronics": (30, 900), "Home": (8, 250), "Beauty": (5, 80), "Sports": (10, 300),
    "Books": (6, 40), "Toys": (7, 90), "Fashion": (12, 220),
}
CHANNELS = [("organic", .3), ("paid_search", .25), ("email", .2), ("social", .15), ("referral", .1)]

def _pick(rng, pairs):
    r, acc = rng.random(), 0
    for v, w in pairs:
        acc += w
        if r <= acc: return v
    return pairs[-1][0]

def generate(customers: int = 3000, days: int = 365, seed: int = 7, out: Path | None = None) -> dict:
    rng = random.Random(seed)
    out = out or config.RAW
    out.mkdir(parents=True, exist_ok=True)
    end = dt.date(2026, 10, 1)
    start = end - dt.timedelta(days=days)

    products = []
    for i in range(1, 241):
        cat = rng.choice(list(CATEGORIES))
        lo, hi = CATEGORIES[cat]
        price = round(rng.uniform(lo, hi), 2)
        products.append({"product_id": f"P{i:04d}", "name": f"{cat} item {i}", "category": cat,
                         "unit_price": price, "unit_cost": round(price * rng.uniform(.4, .75), 2)})
    with open(out / "products.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=products[0].keys()); w.writeheader(); w.writerows(products)

    custs = []
    for i in range(1, customers + 1):
        fn, ln = rng.choice(FIRST), rng.choice(LAST)
        signup = start + dt.timedelta(days=int(rng.random() ** 1.3 * days))
        custs.append({"customer_id": f"C{i:05d}", "full_name": f"{fn} {ln}",
                      "email": f"{fn}.{ln}{i}@example.com".lower() if rng.random() > .03 else "",
                      "country": rng.choice([c for c, _ in COUNTRIES]) if rng.random() < .3 else _pick(rng, COUNTRIES),
                      "signup_date": signup.isoformat(), "acquisition_channel": _pick(rng, CHANNELS)})
    dirty = [dict(c) for c in rng.sample(custs, 25)]   # duplicates with messy casing
    for d in dirty: d["country"] = d["country"].lower(); d["full_name"] = d["full_name"].upper()
    with open(out / "customers.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=custs[0].keys()); w.writeheader(); w.writerows(custs + dirty)

    orders, items, events, oid, iid = [], [], [], 0, 0
    for c in custs:
        signup = dt.date.fromisoformat(c["signup_date"])
        n_orders = int(rng.expovariate(1 / 2.2))
        for _ in range(n_orders):
            od = signup + dt.timedelta(days=int(rng.expovariate(1 / 60)))
            if od > end: continue
            oid += 1
            # seasonality: Nov/Dec lift is implicit via later duplicates
            status = rng.choices(["completed", "returned", "cancelled", "pending"], [.82, .07, .08, .03])[0]
            orders.append({"order_id": f"O{oid:06d}", "customer_id": c["customer_id"],
                           "order_ts": f"{od.isoformat()}T{rng.randint(6,23):02d}:{rng.randint(0,59):02d}:00",
                           "status": status, "payment_method": rng.choice(["card", "paypal", "upi", "gift_card"]),
                           "discount_pct": rng.choice([0, 0, 0, 5, 10, 20])})
            for p in rng.sample(products, rng.randint(1, 4)):
                iid += 1
                items.append({"item_id": f"I{iid:07d}", "order_id": f"O{oid:06d}", "product_id": p["product_id"],
                              "quantity": rng.randint(1, 3)})
        # sessions/events
        for _ in range(rng.randint(2, 14)):
            d = signup + dt.timedelta(days=int(rng.expovariate(1 / 45)))
            if d > end: continue
            step = rng.random()
            for name, p in (("page_view", 1), ("add_to_cart", .35), ("checkout_start", .18), ("purchase", .12)):
                if step > p and name != "page_view": break
                events.append({"event_id": f"E{len(events)+1:08d}", "customer_id": c["customer_id"],
                               "event_name": name, "event_ts": f"{d.isoformat()}T{rng.randint(6,23):02d}:{rng.randint(0,59):02d}:00",
                               "device": rng.choice(["mobile", "desktop", "tablet"])})
    orders += rng.sample(orders, 40)  # duplicated order rows (at-least-once delivery)
    orders.append({**orders[0], "order_id": "O999999", "customer_id": "C99999"})  # orphan FK
    for name, rows in (("orders", orders), ("order_items", items)):
        with open(out / f"{name}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
    with open(out / "events.jsonl", "w") as f:
        for e in events: f.write(json.dumps(e) + "\n")
    return {"customers": len(custs) + len(dirty), "products": len(products), "orders": len(orders),
            "order_items": len(items), "events": len(events)}

if __name__ == "__main__":
    print(generate())
