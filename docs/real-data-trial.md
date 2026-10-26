# Running Matchbook on your own statements

This is a **trial**, not a deployment. The goal is a number you can defend: *how often is the engine right on my data, and how sure am I?*
Your statements have no answer key, so accuracy is **estimated from a random sample that you label by hand**, and reported with a confidence interval.
Everything below runs on your machine. The trial tools need no login, no Postgres and no model server.

## 0. What you can and cannot conclude

| You can say | You cannot say |
|---|---|
| "On *my* three exports for *these months*, N% of auto-matches were wrong (95% interval a–b), from a labelled random sample of M." | "It is accurate." (on what? for whom?) |
| "The engine's books-to-gateway-to-bank totals tie out to the paisa." (control totals) | "It catches fraud." Flags measure *unusual*, and you judge usefulness, not fraud. |
| "X% of open exceptions were really matches the engine missed (interval a–b)." | Anything about other banks, other gateways, or other periods. |

## 1. What to export

Three files for the **same date range** (the periods must overlap or everything will look unmatched):

| File | One row per | Needs |
|---|---|---|
| Ledger (your books / sales) | order or invoice | order id, date, total, and the gateway payment id if you record it |
| Gateway settlement report | captured payment | payment id, order id (if the gateway echoes it), settlement/payout id, captured date, settled date, gross, fee, tax, net |
| Bank statement | transaction | date, narration, **separate** debit and credit columns, running balance |

Export CSV, UTF-8, oldest first if you can choose. Money with a **dot** as decimal separator and no currency symbols.

## 2. Mask first (before anything else touches the files)

```bash
# one key for ALL files, so the same payee / UTR becomes the same token everywhere
.venv/bin/python -m tessera.recon.mask_tool bank.csv gateway.csv ledger.csv \
    --out-dir data/trial/masked --key-file data/trial/mask.key \
    --names-file my_names.txt          # one per line: you, family, staff, regular customers
```
- Tokens are keyed hashes: the same value always gets the same token (repeat-payee rules still work, a UTR in the bank file still equals the UTR in the gateway file) and cannot be reversed without the key.
- **Keep `mask.key` secret and never send it with the masked files.** Re-use the same key if you mask more months, or payees will not line up.
- Amounts, dates and headers are untouched.
- The tool prints anything it still sees (emails, long numbers, PAN). It must say "Read a sample…" and exit 0 before you go on.
- **It cannot be perfect on free text.** Open the masked files and read 50 narrations yourself. Names that are not in your list, addresses, and employer names can survive.
- Use `--id-columns "Account Holder,Customer Name"` to replace a whole column.

## 3. Check each file before importing

```bash
.venv/bin/python -m tessera.recon.trial preflight data/trial/masked/bank.masked.csv --kind bank
```

| Message | Meaning | What to do |
|---|---|---|
| `preamble` / `footer` | title, summary or "total" lines around the table | `trial normalize FILE --out clean.csv` |
| `newest_first` | rows run newest to oldest | `trial normalize FILE --out clean.csv --reverse` |
| `date_ambiguous` | every date has both parts ≤ 12 (03/04 = 3 April or 4 March?) | ask the bank; the importer assumes day first |
| `decimal_comma`, `currency_symbol` | `1.234,50` or `₹1,200` | fix in a spreadsheet; not auto-fixed because guessing is dangerous |
| `single_amount_layout` | one signed Amount column plus Dr/Cr | split into debit and credit columns first (not supported yet) |
| `balance_breaks` | the running balance does not follow the movements | rows are probably **missing** (a lost page) or out of order; look at the examples |
| `gap` | a whole month has no rows | a statement period is missing |
| `duplicate_rows`, `duplicate_ids` | repeated rows / order ids | genuine repeats happen on statements; repeated order ids break matching |

Do not continue past an `ERROR` line.

## 4. Import and read the control totals

```bash
.venv/bin/python -m tessera.recon.trial load --bank data/trial/masked/bank.masked.csv \
    --gateway data/trial/masked/gateway.masked.csv --ledger data/trial/masked/ledger.masked.csv --no-llm
#   prints the proposed column mapping. Read it. Then:
... load ... --accept-mapping
```
Drop `--no-llm` to let the local model propose mappings for unfamiliar headers (needs Ollama). Mappings are always validated against the data.

The tie-out must say **consistent: True**. Those four identities hold by construction, so a failure means an import or engine fault; do not interpret anything else until it is fixed.
Then look at the **value-weighted** match rate, not just the count: 95% of orders matched is a different story if the unmatched 5% holds half the money.

## 5. Draw the sample

```bash
.venv/bin/python -m tessera.recon.trial sample --n 40 --seed 7 \
    --per-stratum "match:L1:B_amount_window=150,match:L1:A_ref=60"
```
This writes `data/trial/workbook.csv` (owner-only) with **random** items from each rule, each queue and each flag type, and `workbook.meta.json` with the population sizes. The seed makes it reproducible. **Do not edit the sampled items**, and do not swap in ones you find more interesting: that biases the estimate.

Spend labels where the risk is. Reference-based passes (`A_*`) are nearly deterministic; the amount-window pass (`B_*`) and payout combinations (`C_*`) are the ones that can be wrong.

### How many labels do you need?
To claim the wrong rate is **below** a target with 95% confidence you need this many *clean* (zero wrong) samples in that group:

| Target | Samples |
|---|---:|
| under 5% | 59 |
| under 2% | 149 |
| under 1% | 299 |
| under 0.5% | 598 |

One wrong item in the sample means the interval is wider and you need more. With only 25 labels the best you can claim is "under about 11%", and the scorer will say so.
Plan on roughly a minute per item. A staged approach works: label ~60 per rule, score, then add more only to the groups whose interval is still too wide.

## 6. Label it

Open `workbook.csv`, fill the `verdict` column using only the words in `question`:

| Group | Question | Words |
|---|---|---|
| `match:*` | Are these two records really the same payment? | `correct`, `wrong`, `unsure` |
| `exception:*` | Genuine discrepancy, or does a match exist the engine missed? | `real`, `missed_match`, `unsure` |
| `flag:*` | Would a careful reviewer want to look at this? | `useful`, `not_useful`, `unsure` |

Decide from **source documents** (the invoice, the gateway dashboard), not from how plausible the engine's pairing looks. Use `unsure` honestly: unsure items are excluded from the rate and **reported**, and a high unsure share makes the result weak. Typos are rejected, not guessed.

## 7. Score and report

```bash
.venv/bin/python -m tessera.recon.trial score      # prints estimates, warns about thin samples
.venv/bin/python -m tessera.recon.trial report     # writes data/trial/report.md
```
Per group you get the labelled count, events, the rate and a Wilson 95% interval (exact bound when zero events). Across groups the estimate is **stratified** (each group weighted by its real size) with a finite-population correction, and the upper bound is deliberately conservative so a clean-looking sample never produces a falsely tight interval. The method itself was validated: its 95% intervals contained the truth ≥ 95% of the time in simulation, and on a synthetic scenario with known answers it recovered them from labelled samples.

## 8. Writing it up honestly

Good: *"On three masked exports covering Jan–Mar 2026 (N orders), I labelled 420 random auto-matches. 1 was wrong: estimated wrong-match rate 0.2% (95% interval 0.0–1.4%). The engine's totals tied out exactly, and 96.8% of order value was matched automatically."*
Bad: *"99.8% accurate."*

Always include: what period and size, how many you labelled, the interval, the unsure count, and that it is one dataset.

## Known limits on real data
- Refunds, partial payments, one payment covering several orders, chargebacks, multi-currency and gateway reserves are **not modelled**. They will appear as exceptions, which is the safe failure, but they lower the match rate.
- One signed amount column, decimal commas and PDF/scanned statements are unsupported (preflight tells you).
- Timestamps are treated as dates; time-zone edges can shift a transaction by a day (the matcher allows a 3-day window).
- Anomaly rules were tested only on synthetic anomalies. Whether they are *useful on your statement* is exactly what the `flag:*` groups measure.
- Delete `data/trial/` when finished. It holds the workbook, scores and the masking key.
