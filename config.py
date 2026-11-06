"""Configurazione centrale di AI Capital (Adaptive Edition: universo dinamico, parametri scelti da Gemini)."""

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
MEMORY_DIR = BASE_DIR / "memory"
CONTRATTO_PATH = BASE_DIR / "contratto.md"
STORICO_COMPLETO_PATH = MEMORY_DIR / "storico_completo.json"
STATO_COMPATTO_PATH = MEMORY_DIR / "stato_compatto.json"
LEARNING_PATH = MEMORY_DIR / "learning.json"

# Endpoint Paper obbligatorio (vincolo fisso, non modificabile da Gemini)
PAPER_ENDPOINT_LOCKED = "https://paper-api.alpaca.markets"

# Universo "seme": scansionato sempre. NON è una whitelist: Gemini può operare su qualsiasi
# titolo USA negoziabile su Alpaca (verificato in tempo reale da La Gabbia).
CORE_WATCHLIST: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "TQQQ", "SOXL", "SPXL", "SQQQ",
    "NVDA", "AAPL", "MSFT", "META", "AMZN", "GOOGL", "TSLA", "AMD", "AVGO", "PLTR", "NFLX", "CRM",
    "COIN", "MSTR", "HOOD", "SOFI", "SHOP",
    "GLD", "SLV", "USO", "TLT", "XLE", "XLF",
)
# Alias retro-compatibile
ALLOWED_TICKERS: frozenset[str] = frozenset(CORE_WATCHLIST)

TICKER_CATEGORIES: dict[str, str] = {
    "SPY": "INDICE", "QQQ": "INDICE", "IWM": "INDICE",
    "TQQQ": "LEVA 3X LONG", "SOXL": "LEVA 3X SEMI", "SPXL": "LEVA 3X S&P", "SQQQ": "HEDGE 3X SHORT",
    "NVDA": "AI & TECH", "AAPL": "MEGA TECH", "MSFT": "MEGA TECH", "META": "AI & TECH",
    "AMZN": "MEGA TECH", "GOOGL": "MEGA TECH", "TSLA": "HIGH BETA", "AMD": "AI & SEMI",
    "AVGO": "AI & SEMI", "PLTR": "AI MOMENTUM", "NFLX": "MOMENTUM", "CRM": "CLOUD TECH",
    "COIN": "CRYPTO PROXY", "MSTR": "CRYPTO HIGH BETA", "HOOD": "FINTECH BETA",
    "SOFI": "FINTECH BETA", "SHOP": "E-COMMERCE",
    "GLD": "ORO / DIFESA", "SLV": "ARGENTO", "USO": "PETROLIO", "TLT": "TREASURY 20Y",
    "XLE": "ENERGIA", "XLF": "FINANZA",
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

# Obiettivo (fisso): +1% netto al giorno. Superarlo è consentito e desiderato.
DAILY_NET_TARGET_PCT: float = float(os.getenv("DAILY_NET_TARGET_PCT", "1.00"))

# Peso di default usato da La Gabbia se non arriva un parametro da Gemini
MAX_SINGLE_ASSET_WEIGHT: float = float(os.getenv("MAX_SINGLE_ASSET_WEIGHT", "0.25"))
# Tetto anti-allucinazione: Gemini sceglie il peso massimo, ma mai oltre questa soglia
HARD_MAX_SINGLE_ASSET_WEIGHT: float = float(os.getenv("HARD_MAX_SINGLE_ASSET_WEIGHT", "0.50"))

# Quanti simboli inviare al prompt (controllo del consumo di token)
MAX_MARKET_SYMBOLS_IN_PROMPT: int = int(os.getenv("MAX_MARKET_SYMBOLS_IN_PROMPT", "28"))

# Parametri iniziali: Gemini li ricalibra a ogni sessione in base ai risultati
DEFAULT_STRATEGY_PARAMS: dict[str, Any] = {
    "max_peso_singolo_asset_pct": 25.0,
    "take_profit_default_pct": 2.2,
    "stop_loss_default_pct": 1.5,
    "max_posizioni_aperte": 8,
    "cash_minimo_pct": 5.0,
    "modalita_dopo_target": "CONTINUA",
    "razionale_parametri": "Parametri iniziali di default (Gemini li ricalibra a ogni sessione).",
}

# Limiti di sanità (solo per scartare valori assurdi/allucinati, non per guidare la strategia)
_PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "max_peso_singolo_asset_pct": (1.0, HARD_MAX_SINGLE_ASSET_WEIGHT * 100.0),
    "take_profit_default_pct": (0.2, 50.0),
    "stop_loss_default_pct": (0.2, 50.0),
    "max_posizioni_aperte": (1, 40),
    "cash_minimo_pct": (0.0, 100.0),
}
_VALID_TARGET_MODES = {"CONTINUA", "PROTEGGI_PROFITTI", "STOP_NUOVI_INGRESSI"}


def sanitize_strategy_params(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Unisce i parametri scelti da Gemini ai default e scarta valori fuori scala."""
    params = dict(DEFAULT_STRATEGY_PARAMS)
    if raw:
        for key, value in raw.items():
            if key in params and value is not None:
                params[key] = value
    for key, (lo, hi) in _PARAM_BOUNDS.items():
        try:
            val = float(params[key])
        except (TypeError, ValueError):
            val = float(DEFAULT_STRATEGY_PARAMS[key])
        val = max(lo, min(hi, val))
        params[key] = int(round(val)) if key == "max_posizioni_aperte" else round(val, 3)
    if params.get("modalita_dopo_target") not in _VALID_TARGET_MODES:
        params["modalita_dopo_target"] = "CONTINUA"
    params["razionale_parametri"] = str(params.get("razionale_parametri", ""))[:240]
    return params


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
