"""Configurazione centrale e costanti inviolabili per AI Capital."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
MEMORY_DIR = BASE_DIR / "memory"
CONTRATTO_PATH = BASE_DIR / "contratto.md"
STORICO_COMPLETO_PATH = MEMORY_DIR / "storico_completo.json"
STATO_COMPATTO_PATH = MEMORY_DIR / "stato_compatto.json"

# Endpoint Paper obbligatorio per La Gabbia
PAPER_ENDPOINT_LOCKED = "https://paper-api.alpaca.markets"

# Watchlist chiusa (8 ticker)
ALLOWED_TICKERS: frozenset[str] = frozenset(
    {"SPY", "QQQ", "IWM", "GLD", "TLT", "AAPL", "MSFT", "NVDA"}
)

# Credenziali e parametri da .env
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL: str = os.getenv("ALPACA_BASE_URL", PAPER_ENDPOINT_LOCKED).rstrip("/")

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Rome")
MAX_SINGLE_ASSET_WEIGHT: float = float(os.getenv("MAX_SINGLE_ASSET_WEIGHT", "0.20"))


def verify_paper_endpoint(url: str | None = None) -> str:
    """Verifica deterministica che l'endpoint sia rigorosamente quello Paper di Alpaca."""
    target = (url if url is not None else ALPACA_BASE_URL).strip().rstrip("/")
    # Accetta anche eventuale suffisso /v2 ma la radice deve essere paper-api.alpaca.markets
    if target.endswith("/v2"):
        target = target[:-3].rstrip("/")
    if target != PAPER_ENDPOINT_LOCKED:
        raise RuntimeError(
            f"VIOLAZIONE LA GABBIA [CRITICA]: Endpoint non autorizzato '{url}'. "
            f"Consentito esclusivamente '{PAPER_ENDPOINT_LOCKED}'."
        )
    return PAPER_ENDPOINT_LOCKED
