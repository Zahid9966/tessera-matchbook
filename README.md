# Tessera and Matchbook

Two local projects on one platform:
- **Tessera**: data-engineering and analytics platform with a local AI analyst (`./run.sh`, http://127.0.0.1:8787).
- **Matchbook**: payment reconciliation and anomaly review with maker/checker approvals (`./run_matchbook.sh`, http://127.0.0.1:8788).

Architecture, measured results, and an honest list of what is and is not verified:
`docs/tessera-architecture-atlas.html` and `docs/matchbook-architecture-atlas.html`.

## Matchbook: local development
```bash
uv venv .venv && uv pip install -e ".[dev]"
./run_matchbook.sh                       # starts a PRIVATE Postgres under data/pgdev (never touches yours), then the app
.venv/bin/python scripts/dev_seed.py     # local users with random passphrases -> data/dev-credentials.txt (owner-only, git-ignored)
```
Sign in at http://127.0.0.1:8788. Roles: viewer (read), reviewer (propose, import), approver (approve), admin (users, demo data).
Every decision is a **proposal** that a *different* approver must approve.

First administrator on any other database (there are no default credentials):
```bash
MATCHBOOK_DATABASE_URL=postgresql://... .venv/bin/python -m tessera.recon.admin create-user alice --role admin
```

## Matchbook: containers (written, not yet built by the author)
```bash
cp .env.example .env            # set POSTGRES_PASSWORD
docker compose up -d --build
docker compose run --rm app python -m tessera.recon.admin create-user alice --role admin
```
Serve it only behind a TLS reverse proxy and set `MATCHBOOK_COOKIE_SECURE=true`.

## Real-data trial and public evaluation
- `docs/real-data-trial.md`: mask, check, import, sample, label, score with confidence intervals (`python -m tessera.recon.mask_tool`, `python -m tessera.recon.trial`).
- `python -m tessera.recon.public_eval`: real orders (UCI Online Retail II, CC BY 4.0, downloaded separately into `data/public/`) with simulated payment rails; results in `data/public/results.json`.

## Checks
```bash
.venv/bin/ruff check .         # includes security rules
.venv/bin/python -m pytest -q  # 118 tests; starts a private Postgres automatically (CI supplies MATCHBOOK_TEST_DATABASE_URL)
```

---

# Tessera (original README section)

A local data-engineering and analytics platform with an AI analyst. Everything runs on your machine.
Architecture: open `docs/tessera-architecture-atlas.html` in a browser.

## Run
```bash
uv venv .venv && uv pip install -e .      # once
.venv/bin/python -m tessera.generate      # synthetic e-commerce sources -> data/raw
./run.sh                                  # starts Ollama (if needed) + http://127.0.0.1:8787
```
First time, open the **Pipelines** tab and click *Run pipeline* (or run `.venv/bin/python -m tessera.cli pipeline` **before** starting the server; DuckDB allows one writer process).

Local models (Ollama): `ollama pull qwen2.5:3b` and `ollama pull nomic-embed-text`.
Better SQL on 16 GB+ RAM: `TESSERA_CHAT_MODEL=qwen2.5:7b ./run.sh`.

## Test
```bash
.venv/bin/python -m pytest -q    # 19 tests, no LLM or network needed
```

## Add your own data
Drop `*.csv`, `*.jsonl` or `*.parquet` into `data/raw/`, add `models/staging/stg_<name>.sql` using `{{ source('<name>') }}`,
build marts with `{{ ref('...') }}`, and declare tests and PII columns in `models/schema.yml`.


---

## Status
A hardened prototype, not a deployed service. Everything marked "verified" in the atlas pages was exercised by an automated test or in the running app; the Dockerfile, compose file and CI workflow were written but **not built or run by the author** (no Docker access on the development machine). Known gaps are listed in `docs/matchbook-architecture-atlas.html`.
All data in this repository is synthetic. Nothing from any real statement is included.

## Data attribution
`python -m tessera.recon.public_eval` evaluates matching on real order amounts from the UCI *Online Retail II* dataset (not included here; download it from the source and place it under `data/public/`).
Chen, D. (2012). *Online Retail II* [Dataset]. UCI Machine Learning Repository. https://doi.org/10.24432/C5CG6D, licensed CC BY 4.0.

## Licence
MIT. See `LICENSE`.
