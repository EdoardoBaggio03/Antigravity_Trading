"""Motore decisionale AI Capital basato sull'SDK ufficiale `google-genai` con output Pydantic strutturato."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, field_validator

import config

logger = logging.getLogger("AICapital.AITrader")


class OrdineProposto(BaseModel):
    """Singolo ordine proposto dall'AI in formato rigorosamente strutturato."""

    ticker: Literal["SPY", "QQQ", "IWM", "GLD", "TLT", "AAPL", "MSFT", "NVDA"] = Field(
        description="Ticker appartenente esclusivamente alla Watchlist Chiusa di 8 asset."
    )
    azione: Literal["BUY", "SELL", "HOLD"] = Field(
        description="Azione operativa (solo posizioni Long: BUY o SELL di asset posseduti)."
    )
    quantita: float = Field(
        ge=0.0,
        description="Numero di azioni/quote da negoziare (>0 per BUY/SELL, 0 per HOLD).",
    )
    acquisto_sui_massimi_senza_ritracciamento: bool = Field(
        default=False,
        description="True se il titolo è sui massimi senza alcun ritracciamento (vieta il BUY).",
    )
    razionale_tecnico: str = Field(
        min_length=5,
        max_length=220,
        description="Sintesi concisa del motivo swing/medio termine.",
    )
    perche_potrei_avere_torto: str = Field(
        min_length=5,
        max_length=250,
        description="OBBLIGATORIO: analisi critica concreta del perché questa operazione potrebbe fallire.",
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
        description="Sintesi macro/tecnica ultra-compatta.",
    )
    intervento_critico_necessario: bool = Field(
        default=False,
        description="Usato a metà seduta (18:30): True solo in caso di emergenza reale.",
    )
    ordini: list[OrdineProposto] = Field(
        default_factory=list,
        description="Lista di ordini operativi (vuota se nessuna azione necessaria).",
    )
    nota_giorno_precedente: str = Field(
        min_length=5,
        max_length=350,
        description="Nota sintetica per la sessione del giorno successivo.",
    )
    nuova_regola_appresa: str = Field(
        default="",
        max_length=180,
        description="Eventuale regola operativa appresa oggi (vuota se nessuna novità).",
    )


SYSTEM_INSTRUCTION_COMPACT = (
    "Sei AI Capital, Senior Quantitative Swing Trader. Rispetta contratto.md: "
    "1) Obiettivo: battere SPY nel medio periodo senza day trading frenetico. "
    "2) Watchlist chiusa: SPY, QQQ, IWM, GLD, TLT, AAPL, MSFT, NVDA. "
    "3) Max 20% Equity per singolo asset, solo posizioni Long coperte da Cash reale. "
    "4) Compila SEMPRE 'perche_potrei_avere_torto' con rischi reali e non comprare sui massimi senza ritracciamento. "
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

    def build_compact_prompt(
        self,
        fase: Literal["morning", "open", "midday", "close"],
        portfolio_summary: dict[str, Any],
        market_snapshot: dict[str, Any],
        compact_memory_json: str,
    ) -> str:
        """Costruisce un prompt compatto (< 2.500 token totali) con stato portafoglio, prezzi e memoria."""
        payload = {
            "fase": fase,
            "portafoglio": {
                "equity": round(float(portfolio_summary.get("equity", 0.0)), 2),
                "cash": round(float(portfolio_summary.get("cash", 0.0)), 2),
                "posizioni": portfolio_summary.get("positions", {}),
            },
            "mercato": market_snapshot,
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

            # Filtra eventuali singoli ordini privi di 'perche_potrei_avere_torto' valido
            # oppure rifiuta l'intero blocco se malformato
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
        """Invoca Gemini (`google-genai`) con `response_schema=DecisioneTradingAI` e `temperature=0.2`."""
        prompt = self.build_compact_prompt(
            fase=fase,
            portfolio_summary=portfolio_summary,
            market_snapshot=market_snapshot,
            compact_memory_json=compact_memory_json,
        )

        fallback_chain = [self.model_name]
        for candidate in ("gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.7-flash"):
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
                logger.error("Errore chiamata Gemini API (%s): %s", model_to_try, exc)
                return None
        return None
