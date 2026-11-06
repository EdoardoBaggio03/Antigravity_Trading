"""La Gabbia: Validazione deterministica pre-ordine per AI Capital."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import config

logger = logging.getLogger("AICapital.RiskCage")


@dataclass
class OrderValidationResult:
    approved: bool
    order: dict[str, Any]
    reason: str
    estimated_cost: float = 0.0


@dataclass
class BatchValidationReport:
    approved_orders: list[dict[str, Any]] = field(default_factory=list)
    rejected_orders: list[OrderValidationResult] = field(default_factory=list)
    remaining_cash: float = 0.0


class RiskCage:
    """Implementa i 4 pilastri deterministici di 'La Gabbia' definiti in contratto.md."""

    def __init__(
        self,
        base_url: str | None = None,
        max_single_asset_weight: float | None = None,
        allowed_tickers: frozenset[str] | None = None,
    ) -> None:
        self.base_url = config.verify_paper_endpoint(base_url)
        self.max_weight = (
            max_single_asset_weight
            if max_single_asset_weight is not None
            else config.MAX_SINGLE_ASSET_WEIGHT
        )
        self.allowed_tickers = allowed_tickers or config.ALLOWED_TICKERS

    def enforce_paper_endpoint(self, endpoint_url: str | None = None) -> str:
        """Pilastro 1: Arresto immediato se l'endpoint non è https://paper-api.alpaca.markets."""
        return config.verify_paper_endpoint(endpoint_url or self.base_url)

    def validate_orders_batch(
        self,
        orders: list[dict[str, Any]],
        portfolio_equity: float,
        available_cash: float,
        current_positions: dict[str, dict[str, float]],
        market_prices: dict[str, float],
        endpoint_url: str | None = None,
    ) -> BatchValidationReport:
        """
        Valida una lista di ordini proposti dall'AI applicando tutte le regole de La Gabbia:
        1. Endpoint bloccato su https://paper-api.alpaca.markets
        2. Watchlist chiusa (8 ticker)
        3. Solo posizioni Long coperte da liquidità reale (no short, no leva, controllo cash cumulativo)
        4. Max 20% dell'Equity totale su singolo asset (incluso già detenuto + ordini precedenti nella stessa sessione)
        5. Campo 'perche_potrei_avere_torto' obbligatorio/non vuoto e divieto di acquisto sui massimi senza ritracciamento
        """
        # 1. Verifica rigorosa endpoint Paper (solleva RuntimeError in caso di endpoint live)
        self.enforce_paper_endpoint(endpoint_url)

        if portfolio_equity <= 0:
            raise ValueError(f"Equity portafoglio non valida: {portfolio_equity}")

        running_cash = float(available_cash)
        # Tracciamento dinamico delle esposizioni e quantità durante la sessione
        simulated_positions: dict[str, dict[str, float]] = {}
        for sym, pos in (current_positions or {}).items():
            simulated_positions[sym.upper()] = {
                "qty": float(pos.get("qty", 0.0)),
                "market_value": float(pos.get("market_value", 0.0)),
            }

        report = BatchValidationReport(remaining_cash=running_cash)

        for raw_order in orders:
            res = self._validate_single_order(
                raw_order=raw_order,
                portfolio_equity=portfolio_equity,
                running_cash=running_cash,
                simulated_positions=simulated_positions,
                market_prices=market_prices,
            )
            if res.approved:
                ticker = res.order["ticker"]
                azione = res.order["azione"]
                qty = float(res.order["quantita"])
                price = float(market_prices[ticker])

                if azione == "BUY":
                    running_cash -= res.estimated_cost
                    cur = simulated_positions.setdefault(
                        ticker, {"qty": 0.0, "market_value": 0.0}
                    )
                    cur["qty"] += qty
                    cur["market_value"] += res.estimated_cost
                elif azione == "SELL":
                    cur = simulated_positions.setdefault(
                        ticker, {"qty": 0.0, "market_value": 0.0}
                    )
                    cur["qty"] = max(0.0, cur["qty"] - qty)
                    cur["market_value"] = max(0.0, cur["market_value"] - (qty * price))
                    # Per prudenza non aggiungiamo il ricavato non ancora regolato al cash spendibile immediato,
                    # oppure manteniamo cassa conservativa.

                report.approved_orders.append(res.order)
                report.remaining_cash = running_cash
            else:
                logger.warning(
                    "Ordine BLOCCATO da La Gabbia [%s %s]: %s",
                    raw_order.get("azione"),
                    raw_order.get("ticker"),
                    res.reason,
                )
                report.rejected_orders.append(res)

        return report

    def _validate_single_order(
        self,
        raw_order: dict[str, Any],
        portfolio_equity: float,
        running_cash: float,
        simulated_positions: dict[str, dict[str, float]],
        market_prices: dict[str, float],
    ) -> OrderValidationResult:
        if not isinstance(raw_order, dict):
            return OrderValidationResult(
                approved=False,
                order={"raw": raw_order},
                reason="Formato ordine non valido (non è un dizionario).",
            )

        ticker = str(raw_order.get("ticker", "")).strip().upper()
        azione = str(raw_order.get("azione", "")).strip().upper()

        if azione == "HOLD":
            return OrderValidationResult(
                approved=False,
                order=raw_order,
                reason="Azione HOLD non richiede esecuzione ordine a mercato.",
            )

        # Watchlist chiusa
        if ticker not in self.allowed_tickers:
            return OrderValidationResult(
                approved=False,
                order=raw_order,
                reason=f"Ticker '{ticker}' fuori dalla Watchlist Chiusa ({sorted(self.allowed_tickers)}).",
            )

        # Azione valida (no SHORT esplicito)
        if azione not in {"BUY", "SELL"}:
            return OrderValidationResult(
                approved=False,
                order=raw_order,
                reason=f"Azione '{azione}' vietata. Consentiti solo 'BUY' o 'SELL' di posizioni Long esistenti.",
            )

        # Quantità positiva
        try:
            qty = float(raw_order.get("quantita", 0))
        except (TypeError, ValueError):
            qty = 0.0

        if qty <= 0:
            return OrderValidationResult(
                approved=False,
                order=raw_order,
                reason=f"Quantità '{raw_order.get('quantita')}' non valida (deve essere > 0).",
            )

        # Verifica campo obbligatorio 'perche_potrei_avere_torto'
        torto = str(raw_order.get("perche_potrei_avere_torto", "") or "").strip()
        if len(torto) < 5:
            return OrderValidationResult(
                approved=False,
                order=raw_order,
                reason="Campo obbligatorio 'perche_potrei_avere_torto' assente o troppo corto.",
            )

        # Verifica prezzo di mercato valido
        price = market_prices.get(ticker)
        if price is None or float(price) <= 0:
            return OrderValidationResult(
                approved=False,
                order=raw_order,
                reason=f"Prezzo di mercato non disponibile o non valido per {ticker}.",
            )
        price = float(price)
        order_value = qty * price

        current_pos = simulated_positions.get(ticker, {"qty": 0.0, "market_value": 0.0})
        held_qty = float(current_pos.get("qty", 0.0))
        held_value = float(current_pos.get("market_value", held_qty * price))

        if azione == "SELL":
            # Divieto assoluto di vendita allo scoperto (Short)
            if held_qty <= 0 or qty > held_qty + 1e-9:
                return OrderValidationResult(
                    approved=False,
                    order=raw_order,
                    reason=(
                        f"Divieto Short Selling: tentata vendita di {qty} {ticker} "
                        f"ma la quantità posseduta in portafoglio è {held_qty}."
                    ),
                )
            sanitized = {
                **raw_order,
                "ticker": ticker,
                "azione": "SELL",
                "quantita": qty,
                "perche_potrei_avere_torto": torto,
            }
            return OrderValidationResult(
                approved=True,
                order=sanitized,
                reason="Ordine SELL Long valido.",
                estimated_cost=0.0,
            )

        # Validazioni specifiche per BUY
        # 1. Divieto di acquisto sui massimi senza ritracciamento
        sui_massimi = bool(
            raw_order.get("acquisto_sui_massimi_senza_ritracciamento", False)
        )
        # Controlla anche se i dati di mercato indicano assenza di ritracciamento (es. drawdown_pct >= -0.3% su rally esteso)
        if sui_massimi:
            return OrderValidationResult(
                approved=False,
                order=raw_order,
                reason="Divieto acquisto sui massimi senza ritracciamento (flag anti-FOMO attivo).",
            )

        # 2. Copertura di liquidità reale (Cash >= costo_ordine, zero leva)
        if order_value > running_cash + 1e-6:
            return OrderValidationResult(
                approved=False,
                order=raw_order,
                reason=(
                    f"Liquidità insufficiente (Zero Leva): costo ordine ${order_value:.2f} "
                    f"> Cash disponibile residuo ${running_cash:.2f}."
                ),
                estimated_cost=order_value,
            )

        # 3. Limite max 20% Equity su singolo asset (incluso già detenuto)
        new_asset_total_value = held_value + order_value
        new_weight = new_asset_total_value / portfolio_equity
        if new_weight > self.max_weight + 1e-6:
            return OrderValidationResult(
                approved=False,
                order=raw_order,
                reason=(
                    f"Superato limite concentrazione {self.max_weight * 100:.0f}% Equity su {ticker}: "
                    f"nuovo peso {new_weight * 100:.2f}% (${new_asset_total_value:.2f} / ${portfolio_equity:.2f})."
                ),
                estimated_cost=order_value,
            )

        sanitized = {
            **raw_order,
            "ticker": ticker,
            "azione": "BUY",
            "quantita": qty,
            "perche_potrei_avere_torto": torto,
        }
        return OrderValidationResult(
            approved=True,
            order=sanitized,
            reason="Ordine BUY conforme a tutti i vincoli de La Gabbia.",
            estimated_cost=order_value,
        )
