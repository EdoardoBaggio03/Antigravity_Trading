"""Configurazione centrale e costanti inviolabili per AI Capital (1% Daily Target Edition)."""

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

# Universo Multi-Asset Autorizzato (30 Strumenti ad Alta Liquidità & Alto Beta)
ALLOWED_TICKERS: frozenset[str] = frozenset(
    {
        # Indici & Leva Tattica 3x (Long & Inverse)
        "SPY",
        "QQQ",
        "IWM",
        "TQQQ",
        "SOXL",
        "SPXL",
        "SQQQ",
        # Mega-Cap & AI Momentum
        "NVDA",
        "AAPL",
        "MSFT",
        "META",
        "AMZN",
        "GOOGL",
        "TSLA",
        "AMD",
        "AVGO",
        "PLTR",
        "NFLX",
        "CRM",
        # High-Beta Fintech & Crypto-Proxy
        "COIN",
        "MSTR",
        "HOOD",
        "SOFI",
        "SHOP",
        # Commodities, Macro & Settori
        "GLD",
        "SLV",
        "USO",
        "TLT",
        "XLE",
        "XLF",
    }
)

TICKER_CATEGORIES: dict[str, str] = {
    "SPY": "INDICE",
    "QQQ": "INDICE",
    "IWM": "INDICE",
    "TQQQ": "LEVA 3X LONG",
    "SOXL": "LEVA 3X SEMI",
    "SPXL": "LEVA 3X S&P",
    "SQQQ": "HEDGE 3X SHORT",
    "NVDA": "AI & TECH",
    "AAPL": "MEGA TECH",
    "MSFT": "MEGA TECH",
    "META": "AI & TECH",
    "AMZN": "MEGA TECH",
    "GOOGL": "MEGA TECH",
    "TSLA": "HIGH BETA",
    "AMD": "AI & SEMI",
    "AVGO": "AI & SEMI",
    "PLTR": "AI MOMENTUM",
    "NFLX": "MOMENTUM",
    "CRM": "CLOUD TECH",
    "COIN": "CRYPTO PROXY",
    "MSTR": "CRYPTO HIGH BETA",
    "HOOD": "FINTECH BETA",
    "SOFI": "FINTECH BETA",
    "SHOP": "E-COMMERCE",
    "GLD": "ORO / DIFESA",
    "SLV": "ARGENTO",
    "USO": "PETROLIO",
    "TLT": "TREASURY 20Y",
    "XLE": "ENERGIA",
    "XLF": "FINANZA",
}

# Credenziali e parametri da .env
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL: str = os.getenv("ALPACA_BASE_URL", PAPER_ENDPOINT_LOCKED).rstrip("/")

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Rome")
MAX_SINGLE_ASSET_WEIGHT: float = float(os.getenv("MAX_SINGLE_ASSET_WEIGHT", "0.25"))

# Parametri Strategici 1% Daily Target
DAILY_NET_TARGET_PCT: float = float(os.getenv("DAILY_NET_TARGET_PCT", "1.00"))
POSITION_TAKE_PROFIT_PCT: float = float(os.getenv("POSITION_TAKE_PROFIT_PCT", "2.20"))
POSITION_STOP_LOSS_PCT: float = float(os.getenv("POSITION_STOP_LOSS_PCT", "-1.50"))


def verify_paper_endpoint(url: str | None = None) -> str:
    """Verifica deterministica che l'endpoint sia rigorosamente quello Paper di Alpaca."""
    target = (url if url is not None else ALPACA_BASE_URL).strip().rstrip("/")
    if target.endswith("/v2"):
        target = target[:-3].rstrip("/")
    if target != PAPER_ENDPOINT_LOCKED:
        raise RuntimeError(
            f"VIOLAZIONE LA GABBIA [CRITICA]: Endpoint non autorizzato '{url}'. "
            f"Consentito esclusivamente '{PAPER_ENDPOINT_LOCKED}'."
        )
    return PAPER_ENDPOINT_LOCKED
