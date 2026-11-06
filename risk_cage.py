"""La Gabbia: validazione deterministica pre-ordine (vincoli fissi + parametri dinamici scelti da Gemini)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import config

logger = logging.getLogger("AICapital.RiskCage")

SYMBOL_FORMAT = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")
AssetValidator = Callable[[str], tuple[bool, str]]


def default_symbol_validator(symbol: str) -> tuple[bool, str]:
    """Validatore offline (solo formato). In produzione si usa broker.check_asset_tradable."""
    if SYMBOL_FORMAT.match(symbol):
        return True, ""
    return False, f"Simbolo '{symbol}' non valido o non negoziabile."


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
    """
    Vincoli FISSI (non modificabili da Gemini):
      1. Endpoint esclusivamente Paper.
      2. Nessuno short: SELL solo fino alla quantità posseduta.
      3. Nessuna leva: ogni BUY coperto da Cash reale (controllo cumulativo nella sessione).
      4. Solo titoli USA attivi, negoziabili e non OTC.
      5. Campo 'perche_potrei_avere_torto' obbligatorio.
    Vincoli DINAMICI (scelti da Gemini, entro un tetto anti-allucinazione):
      peso massimo per asset, numero massimo di posizioni, riserva minima di cassa.
    """

    def __init__(
        self,
        base_url: str | None = None,
        max_single_asset_weight: float | None = None,
        asset_validator: AssetValidator | None = None,
        hard_max_weight: float | None = None,
    ) -> None:
        self.base_url = config.verify_paper_endpoint(base_url)
        self.hard_max_weight = hard_max_weight if hard_max_weight is not None else config.HARD_MAX_SINGLE_ASSET_WEIGHT
        default_w = max_single_asset_weight if max_single_asset_weight is not None else config.MAX_SINGLE_ASSET_WEIGHT
        self.max_weight = min(default_w, self.hard_max_weight)
        self.asset_validator = asset_validator or default_symbol_validator

    def enforce_paper_endpoint(self, endpoint_url: str | None = None) -> str:
        return config.verify_paper_endpoint(endpoint_url or self.base_url)

    def validate_orders_batch(
        self,
        orders: list[dict[str, Any]],
        portfolio_equity: float,
        available_cash: float,
        current_positions: dict[str, dict[str, float]],
        market_prices: dict[str, float],
        endpoint_url: str | None = None,
        max_weight: float | None = None,
        max_positions: int | None = None,
        min_cash_reserve_pct: float = 0.0,
    ) -> BatchValidationReport:
        self.enforce_paper_endpoint(endpoint_url)
        if portfolio_equity <= 0:
            raise ValueError(f"Equity portafoglio non valida: {portfolio_equity}")

        eff_max_weight = min(max_weight if max_weight is not None else self.max_weight, self.hard_max_weight)
        cash_reserve = portfolio_equity * max(0.0, min_cash_reserve_pct) / 100.0
        running_cash = float(available_cash)

        simulated: dict[str, dict[str, float]] = {}
        for sym, pos in (current_positions or {}).items():
            simulated[sym.upper()] = {
                "qty": float(pos.get("qty", 0.0)),
                "market_value": float(pos.get("market_value", 0.0)),
            }
        open_count = sum(1 for p in simulated.values() if p["qty"] > 0)

        # Le vendite vengono valutate prima degli acquisti
        ordered = sorted(
            [o for o in orders],
            key=lambda o: 0 if isinstance(o, dict) and str(o.get("azione", "")).upper() == "SELL" else 1,
        )

        report = BatchValidationReport(remaining_cash=running_cash)
        for raw_order in ordered:
            res = self._validate_single_order(
                raw_order, portfolio_equity, running_cash, cash_reserve, simulated,
                market_prices, eff_max_weight, max_positions, open_count,
            )
            if not res.approved:
                logger.warning("Ordine BLOCCATO da La Gabbia [%s %s]: %s",
                               raw_order.get("azione") if isinstance(raw_order, dict) else "?",
                               raw_order.get("ticker") if isinstance(raw_order, dict) else "?",
                               res.reason)
                report.rejected_orders.append(res)
                continue

            ticker, azione, qty = res.order["ticker"], res.order["azione"], float(res.order["quantita"])
            price = float(market_prices[ticker])
            cur = simulated.setdefault(ticker, {"qty": 0.0, "market_value": 0.0})
            if azione == "BUY":
                if cur["qty"] <= 0:
                    open_count += 1
                running_cash -= res.estimated_cost
                cur["qty"] += qty
                cur["market_value"] += res.estimated_cost
            else:
                cur["qty"] = max(0.0, cur["qty"] - qty)
                cur["market_value"] = max(0.0, cur["market_value"] - qty * price)
                if cur["qty"] <= 0:
                    open_count = max(0, open_count - 1)
            report.approved_orders.append(res.order)
            report.remaining_cash = running_cash
        return report

    def _validate_single_order(
        self,
        raw_order: Any,
        equity: float,
        running_cash: float,
        cash_reserve: float,
        simulated: dict[str, dict[str, float]],
        market_prices: dict[str, float],
        max_weight: float,
        max_positions: int | None,
        open_count: int,
    ) -> OrderValidationResult:
        if not isinstance(raw_order, dict):
            return OrderValidationResult(False, {"raw": raw_order}, "Formato ordine non valido.")

        ticker = str(raw_order.get("ticker", "")).strip().upper()
        azione = str(raw_order.get("azione", "")).strip().upper()

        if azione == "HOLD":
            return OrderValidationResult(False, raw_order, "HOLD: nessuna esecuzione richiesta.")
        if azione not in {"BUY", "SELL"}:
            return OrderValidationResult(False, raw_order, f"Azione '{azione}' vietata (consentiti solo BUY o SELL di posizioni Long).")

        try:
            qty = float(raw_order.get("quantita", 0))
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            return OrderValidationResult(False, raw_order, f"Quantità '{raw_order.get('quantita')}' non valida.")

        torto = str(raw_order.get("perche_potrei_avere_torto", "") or "").strip()
        if len(torto) < 5:
            return OrderValidationResult(False, raw_order, "Campo obbligatorio 'perche_potrei_avere_torto' assente o troppo corto.")

        price = market_prices.get(ticker)
        if price is None or float(price) <= 0:
            return OrderValidationResult(False, raw_order, f"Prezzo di mercato non disponibile per {ticker}.")
        price = float(price)

        cur = simulated.get(ticker, {"qty": 0.0, "market_value": 0.0})
        held_qty = float(cur.get("qty", 0.0))
        held_value = float(cur.get("market_value", held_qty * price))

        if azione == "SELL":
            if held_qty <= 0 or qty > held_qty + 1e-9:
                return OrderValidationResult(
                    False, raw_order,
                    f"Divieto Short Selling: vendita di {qty} {ticker} ma quantità posseduta {held_qty}.",
                )
            return OrderValidationResult(True, {**raw_order, "ticker": ticker, "azione": "SELL",
                                                "quantita": qty, "perche_potrei_avere_torto": torto},
                                         "SELL Long valido.")

        # ---------- BUY
        ok, why = self.asset_validator(ticker)
        if not ok:
            return OrderValidationResult(False, raw_order, f"Titolo non ammesso: {why}")

        tp = float(raw_order.get("take_profit_price") or 0.0)
        sl = float(raw_order.get("stop_loss_price") or 0.0)
        if tp > 0 or sl > 0:
            if tp <= price * 1.001 or sl <= 0 or sl >= price * 0.999:
                return OrderValidationResult(
                    False, raw_order,
                    f"Livelli bracket incoerenti su {ticker}: TP {tp} / SL {sl} vs prezzo {price}.",
                )
            qty = float(int(qty))
            if qty < 1:
                return OrderValidationResult(False, raw_order, "Ordine bracket richiede almeno 1 azione intera.")

        if max_positions is not None and held_qty <= 0 and open_count >= max_positions:
            return OrderValidationResult(False, raw_order, f"Raggiunto il numero massimo di posizioni ({max_positions}).")

        cost = qty * price
        spendable = running_cash - cash_reserve
        if cost > spendable + 1e-6:
            return OrderValidationResult(
                False, raw_order,
                f"Liquidità insufficiente (Zero Leva): costo ordine ${cost:.2f} > Cash disponibile residuo ${spendable:.2f}.",
                cost,
            )

        new_weight = (held_value + cost) / equity
        if new_weight > max_weight + 1e-6:
            return OrderValidationResult(
                False, raw_order,
                f"Superato limite concentrazione {max_weight * 100:.0f}% Equity su {ticker}: "
                f"nuovo peso {new_weight * 100:.2f}% (${held_value + cost:.2f} / ${equity:.2f}).",
                cost,
            )

        return OrderValidationResult(
            True,
            {**raw_order, "ticker": ticker, "azione": "BUY", "quantita": qty, "perche_potrei_avere_torto": torto},
            "BUY conforme a La Gabbia.",
            cost,
        )
