from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(os.environ.get("TESSERA_HOME", Path(__file__).resolve().parent.parent))
DATA = ROOT / "data"
RAW = DATA / "raw"
WAREHOUSE = Path(os.environ["TESSERA_WAREHOUSE"]) if os.environ.get("TESSERA_WAREHOUSE") else DATA / "warehouse.duckdb"
MODELS_DIR = ROOT / "models"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

# capability -> model. Call sites name a capability, never a raw model id.
MODEL_CATALOG = {
    "analystChat":   {"model": os.environ.get("TESSERA_CHAT_MODEL", "qwen2.5:3b"), "temperature": 0.1, "restricted": True},
    "sqlWriter":     {"model": os.environ.get("TESSERA_SQL_MODEL", "qwen2.5:3b"), "temperature": 0.0, "restricted": True},
    "insightSummary":{"model": os.environ.get("TESSERA_CHAT_MODEL", "qwen2.5:3b"), "temperature": 0.3, "restricted": True},
    "memoryExtract": {"model": os.environ.get("TESSERA_CHAT_MODEL", "qwen2.5:3b"), "temperature": 0.0, "restricted": True},
    "mappingProposal": {"model": os.environ.get("TESSERA_CHAT_MODEL", "qwen2.5:3b"), "temperature": 0.0, "restricted": True},
    "flagExplain":   {"model": os.environ.get("TESSERA_CHAT_MODEL", "qwen2.5:3b"), "temperature": 0.1, "restricted": True},
    "embeddings":    {"model": os.environ.get("TESSERA_EMBED_MODEL", "nomic-embed-text"), "dims": 768, "restricted": True},
}

def ensure_dirs() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
