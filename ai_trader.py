"""Motore decisionale AI Capital (1% Daily Target Edition) basato su `google-genai` con output Pydantic strutturato."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, field_validator

import config

logger = logging.getLogger("AICapital.AITrader")

AllowedTickerLiteral = Literal[
    "SPY",
    "QQQ",
    "IWM",
    "TQQQ",
    "SOXL",
    "SPXL",
    "SQQQ",
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
    "COIN",
    "MSTR",
    "HOOD",
    "SOFI",
    "SHOP",
    "GLD",
    "SLV",
    "USO",
    "TLT",
    "XLE",
    "XLF",
]


class OrdineProposto(BaseModel):
    """Singolo ordine proposto dall'AI in formato rigorosamente strutturato."""

    ticker: AllowedTickerLiteral = Field(
        description="Ticker appartenente all'universo Multi-Asset di 30 strumenti autorizzati."
    )
    azione: Literal["BUY", "SELL", "HOLD"] = Field(
        description="Azione operativa (solo posizioni Long: BUY o SELL di asset posseduti; usa SQQQ per guadagnare dai ribassi)."
    )
    quantita: float = Field(
        ge=0.0,
        description="Numero di azioni/quote da negoziare (>0 per BUY/SELL, 0 per HOLD).",
    )
    strategia: Literal[
        "MOMENTUM_BREAKOUT",
        "PULLBACK_REVERSAL",
        "TAKE_PROFIT_1PCT",
        "STOP_LOSS_DEFENSE",
        "INVERSE_HEDGE",
    ] = Field(
        default="PULLBACK_REVERSAL",
        description="Strategia tattica applicata per contribuire al target del +1% giornaliero.",
    )
    acquisto_sui_massimi_senza_ritracciamento: bool = Field(
        default=False,
        description="True SOLO se è un acquisto FOMO cieco su ipercomprato estremo senza catalizzatore.",
    )
    razionale_tecnico: str = Field(
        min_length=5,
        max_length=220,
        description="Sintesi concisa del setup tecnico/quantitativo.",
    )
    perche_potrei_avere_torto: str = Field(
        min_length=5,
        max_length=250,
        description="OBBLIGATORIO: analisi critica concreta del rischio contrario e livello di stop.",
    )

    @field_validator("perche_potrei_avere_torto")
    @classmethod
    def check_non_empty_torto(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if len(cleaned) < 5:
            raise ValueError(
                "Il campo 'perche_potrei_avere_torto' non può essere vuoto o banale."
            )
        return cleaned


class DecisioneTradingAI(BaseModel):
    """Schema JSON restituito da Gemini con zero testo superfluo."""

    fase_sessione: Literal["morning", "open", "midday", "close"]
    sentiment_mercato: str = Field(
        min_length=5,
        max_length=240,
        description="Sintesi macro, volatilità e direzione prevalente (Risk-On vs Risk-Off).",
    )
    intervento_critico_necessario: bool = Field(
        default=False,
        description="True a metà seduta se serve prendere profitto (+1% raggiunto), tagliare perdite o ruotare.",
    )
    ordini: list[OrdineProposto] = Field(
        default_factory=list,
        description="Lista di ordini operativi (BUY per entrare, SELL per incassare profitti o chiudere debolezza).",
    )
    nota_giorno_precedente: str = Field(
        min_length=5,
        max_length=350,
        description="Nota operativa per la sessione successiva con focus sul target +1% netto giornaliero.",
    )
    nuova_regola_appresa: str = Field(
        default="",
        max_length=180,
        description="Eventuale regola operativa appresa oggi (vuota se nessuna novità).",
    )


SYSTEM_INSTRUCTION_COMPACT = (
    "Sei AI Capital (1% Daily Target Edition), Senior Quantitative Momentum & Tactical Trader. "
    "Obiettivo primario: realizzare al netto +1.00% al giorno sull'Equity totale. "
    "Regole operative: "
    "1) Universo di 30 ticker ad alta liquidità/beta (inclusi ETF 3x TQQQ, SOXL, SPXL per fasi rialziste e SQQQ per guadagnare nei crolli di mercato senza short selling). "
    "2) Max peso per singolo asset indicato nel prompt (default 20-25% Equity), solo acquisti coperti da Cash reale. "
    "3) Gestione attiva: proponi SELL per incassare profitti quando il portafoglio si avvicina/supera +1.00% giornaliero o una posizione guadagna > +2%, e taglia subito le posizioni deboli (< -1.5%). "
    "4) Compila SEMPRE 'perche_potrei_avere_torto' con rischi reali e non fare acquisti FOMO ciechi (imposta acquisto_sui_massimi_senza_ritracciamento=false sui setup validi). "
    "Rispondi SOLO in JSON conforme allo schema."
)


class AITrader:
    """Interfaccia verso Google AI Studio (`google-genai`) ottimizzata per il Free Tier."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else config.GEMINI_API_KEY
        self.model_name = model_name or config.GEMINI_MODEL
        self._client = client

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(
                    retry_options=types.HttpRetryOptions(attempts=1)
                ),
            )
        return self._client

    def _select_top_market_opportunities(
        self,
        market_snapshot: dict[str, Any],
        held_tickers: set[str],
        max_tickers: int = 16,
    ) -> dict[str, Any]:
        """Seleziona i benchmark, i titoli già in portafoglio e i Top Mover per restare sempre < 2.500 token."""
        if len(market_snapshot) <= max_tickers:
            return market_snapshot

        priority = {"SPY", "QQQ", "TQQQ", "SQQQ", "SOXL", "NVDA", "TSLA", "GLD"} | set(
            held_tickers
        )
        selected: dict[str, Any] = {}
        for sym in priority:
            if sym in market_snapshot:
                selected[sym] = market_snapshot[sym]

        # Ordina i restanti per ampiezza di movimento giornaliero (opportunità di +1% intraday)
        remaining = [
            (sym, info)
            for sym, info in market_snapshot.items()
            if sym not in selected
        ]
        remaining.sort(
            key=lambda x: abs(float(x[1].get("daily_change_pct", 0.0))),
            reverse=True,
        )
        for sym, info in remaining:
            if len(selected) >= max_tickers:
                break
            selected[sym] = info

        return selected

    def build_compact_prompt(
        self,
        fase: Literal["morning", "open", "midday", "close"],
        portfolio_summary: dict[str, Any],
        market_snapshot: dict[str, Any],
        compact_memory_json: str,
    ) -> str:
        """Costruisce un prompt compatto (< 2.500 token totali) con focus sul target +1% giornaliero."""
        equity = float(portfolio_summary.get("equity", 0.0))
        last_equity = float(portfolio_summary.get("last_equity") or equity or 100000.0)
        daily_pnl = equity - last_equity
        daily_pnl_pct = (daily_pnl / last_equity * 100.0) if last_equity > 0 else 0.0
        positions = portfolio_summary.get("positions", {}) or {}

        filtered_market = self._select_top_market_opportunities(
            market_snapshot=market_snapshot,
            held_tickers=set(positions.keys()),
        )

        payload = {
            "fase": fase,
            "target_giornaliero": {
                "obiettivo_netto_pct": config.DAILY_NET_TARGET_PCT,
                "pnl_oggi_usd": round(daily_pnl, 2),
                "pnl_oggi_pct": round(daily_pnl_pct, 2),
                "target_raggiunto": daily_pnl_pct >= config.DAILY_NET_TARGET_PCT,
                "max_peso_singolo_asset_pct": round(
                    config.MAX_SINGLE_ASSET_WEIGHT * 100, 1
                ),
            },
            "portafoglio": {
                "equity": round(equity, 2),
                "cash": round(float(portfolio_summary.get("cash", 0.0)), 2),
                "posizioni": positions,
            },
            "mercato_top_opportunità": filtered_market,
            "memoria_compatta": json.loads(compact_memory_json)
            if compact_memory_json
            else {},
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def parse_and_validate_ai_response(
        self, raw_text: str | dict[str, Any]
    ) -> DecisioneTradingAI | None:
        """
        Valida la risposta JSON dell'AI contro lo schema Pydantic.
        Se il JSON è corrotto o se un ordine ha `perche_potrei_avere_torto` vuoto,
        intercetta l'errore in modo sicuro senza crash.
        """
        try:
            if isinstance(raw_text, dict):
                data = raw_text
            else:
                if not raw_text or not str(raw_text).strip():
                    raise ValueError("Risposta AI vuota.")
                data = json.loads(raw_text)

            decision = DecisioneTradingAI.model_validate(data)
            return decision
        except Exception as exc:
            logger.error(
                "Risposta AI bloccata (JSON corrotto o vincolo schema violato): %s", exc
            )
            return None

    def generate_decision(
        self,
        fase: Literal["morning", "open", "midday", "close"],
        portfolio_summary: dict[str, Any],
        market_snapshot: dict[str, Any],
        compact_memory_json: str,
    ) -> DecisioneTradingAI | None:
        """Invoca Gemini (`google-genai`) con `response_schema=DecisioneTradingAI` e fallback rapido."""
        prompt = self.build_compact_prompt(
            fase=fase,
            portfolio_summary=portfolio_summary,
            market_snapshot=market_snapshot,
            compact_memory_json=compact_memory_json,
        )

        fallback_chain = [self.model_name]
        for candidate in (
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.7-flash",
        ):
            if candidate not in fallback_chain:
                fallback_chain.append(candidate)

        for model_to_try in fallback_chain:
            try:
                response = self.client.models.generate_content(
                    model=model_to_try,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION_COMPACT,
                        temperature=0.2,
                        response_mime_type="application/json",
                        response_schema=DecisioneTradingAI,
                    ),
                )
                raw_output = getattr(response, "text", None)
                return self.parse_and_validate_ai_response(raw_output)
            except Exception as exc:
                err_str = str(exc)
                if "503" in err_str or "UNAVAILABLE" in err_str or "429" in err_str:
                    logger.warning(
                        "Modello %s momentaneamente sovraccarico (503/429), passo al fallback successivo...",
                        model_to_try,
                    )
                    continue
                logger.error(
                    "Errore chiamata Gemini API (%s): %s", model_to_try, exc
                )
                return None
        return None
