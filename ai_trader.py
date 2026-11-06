"""Motore decisionale AI Capital (Adaptive Edition): Gemini sceglie titoli, timing e parametri imparando dai risultati."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, field_validator

import config

logger = logging.getLogger("AICapital.AITrader")

Strategia = Literal[
    "MOMENTUM_BREAKOUT",
    "PULLBACK_REVERSAL",
    "GAP_AND_GO",
    "MEAN_REVERSION",
    "NEWS_CATALYST",
    "INVERSE_HEDGE",
    "ROTAZIONE",
    "TAKE_PROFIT",
    "STOP_LOSS",
    "ALTRO",
]


class ParametriStrategia(BaseModel):
    """Parametri operativi scelti da Gemini (sanificati da config.sanitize_strategy_params)."""

    max_peso_singolo_asset_pct: float = Field(default=25.0, ge=1, le=100, description="Peso massimo % dell'Equity su un singolo titolo.")
    take_profit_default_pct: float = Field(default=2.2, ge=0.2, le=50, description="Take profit % di default se l'ordine non specifica un prezzo.")
    stop_loss_default_pct: float = Field(default=1.5, ge=0.2, le=50, description="Distanza % dello stop loss di default (numero positivo).")
    max_posizioni_aperte: int = Field(default=8, ge=1, le=40, description="Numero massimo di posizioni aperte contemporaneamente.")
    cash_minimo_pct: float = Field(default=5.0, ge=0, le=100, description="Riserva minima di liquidità % dell'Equity.")
    modalita_dopo_target: Literal["CONTINUA", "PROTEGGI_PROFITTI", "STOP_NUOVI_INGRESSI"] = Field(
        default="CONTINUA",
        description="Comportamento quando il P&L di oggi ha superato +1%: continuare, incassare i vincitori o non aprire nuove posizioni.",
    )
    razionale_parametri: str = Field(default="", max_length=240, description="Perché hai scelto questi parametri (citando le statistiche).")


class OrdineProposto(BaseModel):
    """Singolo ordine proposto da Gemini."""

    ticker: str = Field(min_length=1, max_length=10, description="Qualsiasi azione/ETF USA negoziabile (es. NVDA, SOXL, SMCI).")
    azione: Literal["BUY", "SELL", "HOLD"] = Field(description="BUY per entrare, SELL per chiudere (solo quantità possedute), HOLD per nessuna azione.")
    quantita: float = Field(ge=0.0, description="Numero di azioni. Per BUY usa numeri interi.")
    strategia: Strategia = Field(default="ALTRO", description="Strategia usata: serve al motore di apprendimento.")
    take_profit_price: float = Field(default=0.0, ge=0.0, description="Prezzo assoluto di take profit per BUY (0 = usa il default %).")
    stop_loss_price: float = Field(default=0.0, ge=0.0, description="Prezzo assoluto di stop loss per BUY (0 = usa il default %).")
    confidenza: float = Field(default=0.5, ge=0.0, le=1.0, description="Probabilità stimata di successo (0-1).")
    razionale_tecnico: str = Field(min_length=5, max_length=220, description="Setup e motivazione sintetica.")
    perche_potrei_avere_torto: str = Field(min_length=5, max_length=250, description="OBBLIGATORIO: scenario avverso e livello di invalidazione.")

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("perche_potrei_avere_torto")
    @classmethod
    def check_non_empty_torto(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if len(cleaned) < 5:
            raise ValueError("Il campo 'perche_potrei_avere_torto' non può essere vuoto o banale.")
        return cleaned


class DecisioneTradingAI(BaseModel):
    """Schema JSON restituito da Gemini."""

    fase_sessione: Literal["morning", "intraday", "open", "midday", "close"]
    sentiment_mercato: str = Field(min_length=5, max_length=240, description="Regime di mercato (Risk-On/Risk-Off, volatilità, direzione).")
    intervento_critico_necessario: bool = Field(default=False)
    ordini: list[OrdineProposto] = Field(default_factory=list, description="Ordini da eseguire ora (vuoto se nessuna occasione valida).")
    parametri: ParametriStrategia = Field(default_factory=ParametriStrategia, description="Parametri operativi aggiornati.")
    ticker_da_osservare: list[str] = Field(default_factory=list, description="Fino a 15 titoli da monitorare nei prossimi cicli.")
    nota_giorno_precedente: str = Field(min_length=5, max_length=350, description="Nota operativa per i prossimi cicli/giorni.")
    nuova_regola_appresa: str = Field(default="", max_length=180, description="Regola concreta appresa dai risultati (vuota se nessuna).")


SYSTEM_INSTRUCTION = (
    "Sei AI Capital, trader quantitativo autonomo su conto Paper Alpaca. "
    f"OBIETTIVO: +{config.DAILY_NET_TARGET_PCT:.2f}% netto al giorno sull'Equity; superarlo è positivo. "
    "LIBERTA': scegli tu titoli (qualsiasi azione/ETF USA negoziabile, inclusi ETF a leva e inversi come SQQQ/SOXS per guadagnare nei ribassi), "
    "momento di ingresso, dimensione, take profit, stop loss e tutti i parametri in 'parametri'. "
    "Vieni chiamato più volte al giorno: entra solo quando il rapporto rischio/rendimento è favorevole, altrimenti restituisci ordini vuoti. "
    "APPRENDIMENTO: 'apprendimento' contiene i tuoi risultati reali (per strategia, ticker, ora di ingresso Rome). "
    "Aumenta l'uso di ciò che ha aspettativa positiva, riduci o elimina ciò che perde, e spiega in 'razionale_parametri' come le statistiche guidano le scelte. "
    "VINCOLI FISSI (non aggirabili): nessuno short (SELL solo fino alle quantità possedute), nessuna leva (costo BUY <= cash), "
    f"peso per titolo mai oltre {config.HARD_MAX_SINGLE_ASSET_WEIGHT * 100:.0f}% dell'Equity, niente OTC. "
    "Per i BUY usa quantità intere e prezzi take_profit_price > prezzo > stop_loss_price (diventano un ordine bracket gestito dal broker). "
    "Calcola la quantità come floor(Equity * peso% / prezzo). Compila sempre 'perche_potrei_avere_torto'. "
    "Formato mercato: sym=[prezzo, var% oggi, % dai massimi di oggi]; posizioni: sym=[qty, prezzo carico, prezzo, P&L%]. "
    "Rispondi SOLO con JSON conforme allo schema."
)


class AITrader:
    """Interfaccia verso Google AI Studio (`google-genai`) con fallback rapido tra modelli."""

    def __init__(self, api_key: str | None = None, model_name: str | None = None, client: Any | None = None) -> None:
        self.api_key = api_key if api_key is not None else config.GEMINI_API_KEY
        self.model_name = model_name or config.GEMINI_MODEL
        self._client = client

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
            )
        return self._client

    @staticmethod
    def _select_market(
        market_snapshot: dict[str, Any],
        priority: list[str],
        max_symbols: int,
    ) -> dict[str, list[float]]:
        """Seleziona i simboli prioritari + i maggiori movimenti della giornata (controllo token)."""
        chosen: list[str] = [s for s in dict.fromkeys(priority) if s in market_snapshot]
        rest = sorted(
            (s for s in market_snapshot if s not in chosen),
            key=lambda s: abs(float(market_snapshot[s].get("daily_change_pct", 0.0))),
            reverse=True,
        )
        for s in rest:
            if len(chosen) >= max_symbols:
                break
            chosen.append(s)
        return {
            s: [market_snapshot[s].get("price"), market_snapshot[s].get("daily_change_pct"),
                market_snapshot[s].get("pullback_from_high_pct")]
            for s in chosen[:max_symbols]
        }

    def build_compact_prompt(
        self,
        fase: str,
        portfolio_summary: dict[str, Any],
        market_snapshot: dict[str, Any],
        compact_memory_json: str,
        learning_summary: dict[str, Any] | None = None,
        strategy_params: dict[str, Any] | None = None,
        movers: dict[str, list[str]] | None = None,
        now_rome: str | None = None,
    ) -> str:
        equity = float(portfolio_summary.get("equity", 0.0))
        last_equity = float(portfolio_summary.get("last_equity") or equity or 100000.0)
        daily_pnl = equity - last_equity
        daily_pnl_pct = (daily_pnl / last_equity * 100.0) if last_equity > 0 else 0.0
        positions = portfolio_summary.get("positions", {}) or {}
        memory = json.loads(compact_memory_json) if compact_memory_json else {}

        movers = movers or {}
        priority = [*positions.keys(), "SPY", "QQQ", *memory.get("watchlist_dinamica", []),
                    *movers.get("gainers", [])[:5], *movers.get("losers", [])[:3]]

        payload = {
            "fase": fase,
            "ora_rome": now_rome,
            "target": {
                "obiettivo_pct": config.DAILY_NET_TARGET_PCT,
                "pnl_oggi_usd": round(daily_pnl, 2),
                "pnl_oggi_pct": round(daily_pnl_pct, 2),
                "mancante_usd": round(max(0.0, last_equity * config.DAILY_NET_TARGET_PCT / 100.0 - daily_pnl), 2),
            },
            "conto": {"equity": round(equity, 2), "cash": round(float(portfolio_summary.get("cash", 0.0)), 2)},
            "posizioni": {
                s: [round(p.get("qty", 0.0), 4), round(p.get("avg_entry_price", 0.0), 2),
                    round(p.get("current_price", 0.0), 2), round(float(p.get("unrealized_plpc", 0.0)) * 100.0, 2)]
                for s, p in positions.items()
            },
            "parametri_attuali": strategy_params or {},
            "mercato": self._select_market(market_snapshot, priority, config.MAX_MARKET_SYMBOLS_IN_PROMPT),
            "scanner": {k: v[:8] for k, v in movers.items() if v},
            "apprendimento": learning_summary or {},
            "memoria": memory,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def parse_and_validate_ai_response(self, raw_text: str | dict[str, Any] | None) -> DecisioneTradingAI | None:
        """Valida la risposta contro lo schema Pydantic; JSON corrotti o campi vuoti -> None, senza crash."""
        try:
            if isinstance(raw_text, dict):
                data = raw_text
            else:
                if not raw_text or not str(raw_text).strip():
                    raise ValueError("Risposta AI vuota.")
                data = json.loads(raw_text)
            return DecisioneTradingAI.model_validate(data)
        except Exception as exc:
            logger.error("Risposta AI bloccata (JSON corrotto o vincolo schema violato): %s", exc)
            return None

    def generate_decision(
        self,
        fase: str,
        portfolio_summary: dict[str, Any],
        market_snapshot: dict[str, Any],
        compact_memory_json: str,
        learning_summary: dict[str, Any] | None = None,
        strategy_params: dict[str, Any] | None = None,
        movers: dict[str, list[str]] | None = None,
        now_rome: str | None = None,
    ) -> DecisioneTradingAI | None:
        prompt = self.build_compact_prompt(
            fase, portfolio_summary, market_snapshot, compact_memory_json,
            learning_summary, strategy_params, movers, now_rome,
        )
        logger.info("Prompt Gemini: ~%d token stimati", len(prompt) // 4)

        chain = [self.model_name]
        for candidate in ("gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.7-flash"):
            if candidate not in chain:
                chain.append(candidate)

        for model_to_try in chain:
            try:
                response = self.client.models.generate_content(
                    model=model_to_try,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION,
                        temperature=0.2,
                        response_mime_type="application/json",
                        response_schema=DecisioneTradingAI,
                    ),
                )
                return self.parse_and_validate_ai_response(getattr(response, "text", None))
            except Exception as exc:
                err = str(exc)
                if "503" in err or "UNAVAILABLE" in err or "429" in err:
                    logger.warning("Modello %s sovraccarico (503/429), passo al fallback...", model_to_try)
                    continue
                logger.error("Errore chiamata Gemini API (%s): %s", model_to_try, exc)
                return None
        return None
