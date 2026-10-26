#!/usr/bin/env bash
# Matchbook for local development on http://127.0.0.1:8788
# Starts a PRIVATE Postgres cluster under data/pgdev (it never touches any Postgres you already run), then the app.
# First time only: create an administrator:   .venv/bin/python -m tessera.recon.admin create-user NAME --role admin
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/ollama/bin:$HOME/.local/bin:$PATH" OLLAMA_MODELS="$HOME/.local/ollama/models"
export TESSERA_WAREHOUSE="${TESSERA_WAREHOUSE:-$PWD/data/matchbook.duckdb}"
export MATCHBOOK_DATABASE_URL="${MATCHBOOK_DATABASE_URL:-$(.venv/bin/python -m tessera.recon.pg "$PWD/data/pgdev")}"
export MATCHBOOK_ENABLE_DEMO="${MATCHBOOK_ENABLE_DEMO:-true}"
curl -s -m 2 localhost:11434 >/dev/null || (nohup ollama serve >/tmp/ollama.log 2>&1 &)
exec .venv/bin/python -m tessera.recon.app
