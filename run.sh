#!/usr/bin/env bash
# Start Ollama (if not running) and the Tessera server.
cd "$(dirname "$0")"
export PATH="$HOME/.local/ollama/bin:$HOME/.local/bin:$PATH" OLLAMA_MODELS="$HOME/.local/ollama/models"
curl -s -m 2 localhost:11434 >/dev/null || (nohup ollama serve >/tmp/ollama.log 2>&1 &)
exec .venv/bin/python -m tessera.cli serve "$@"
