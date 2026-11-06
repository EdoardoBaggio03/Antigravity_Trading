"""Client Alpaca Paper Trading: universo dinamico, ordini bracket (TP/SL lato server), fill per l'apprendimento."""

from __future__ import annotations

import logging
import math
import re
import time
from typing import Any

import requests

import config

logger = logging.getLogger("AICapital.BrokerAlpaca")

SYMBOL_RE = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")


def _round_price(value: float) -> float:
    """Arrotonda al tick consentito da Alpaca (2 decimali sopra $1, 4 decimali sotto)."""
    return round(value, 2 if value >= 1 else 4)


class AlpacaPaperBroker:
    """Wrapper sicuro per Alpaca Paper Trading (bloccato su https://paper-api.alpaca.markets)."""

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 10,
    ) -> None:
        self.base_url = config.verify_paper_endpoint(base_url or config.ALPACA_BASE_URL)
        self.api_key = api_key if api_key is not None else config.ALPACA_API_KEY
        self.secret_key = secret_key if secret_key is not None else config.ALPACA_SECRET_KEY
        self.timeout = timeout
        self.data_url = "https://data.alpaca.markets"
        self._asset_cache: dict[str, tuple[bool, str]] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------ stato mercato / conto
    def is_market_open(self) -> dict[str, Any]:
        """Verifica se il mercato USA è aperto (gestisce festività, weekend e timeout senza crash)."""
        config.verify_paper_endpoint(self.base_url)
        try:
            resp = requests.get(f"{self.base_url}/v2/clock", headers=self._headers(), timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            return {
                "ok": True,
                "is_open": bool(data.get("is_open", False)),
                "next_open": data.get("next_open"),
                "next_close": data.get("next_close"),
                "timestamp": data.get("timestamp"),
                "error": None,
            }
        except Exception as exc:
            logger.error("Errore o timeout durante verifica clock Alpaca: %s", exc)
            return {"ok": False, "is_open": False, "next_open": None, "next_close": None,
                    "timestamp": None, "error": str(exc)}

    def get_portfolio_state(self) -> dict[str, Any]:
        """Recupera Equity, Cash e posizioni aperte su Alpaca Paper in modo resiliente."""
        config.verify_paper_endpoint(self.base_url)
        try:
            acc_resp = requests.get(f"{self.base_url}/v2/account", headers=self._headers(), timeout=self.timeout)
            acc_resp.raise_for_status()
            acc = acc_resp.json()

            pos_resp = requests.get(f"{self.base_url}/v2/positions", headers=self._headers(), timeout=self.timeout)
            pos_resp.raise_for_status()

            positions: dict[str, dict[str, float]] = {}
            for p in pos_resp.json():
                sym = str(p.get("symbol", "")).upper()
                positions[sym] = {
                    "qty": float(p.get("qty", 0.0)),
                    "qty_available": float(p.get("qty_available", p.get("qty", 0.0)) or 0.0),
                    "market_value": float(p.get("market_value", 0.0)),
                    "avg_entry_price": float(p.get("avg_entry_price", 0.0)),
                    "current_price": float(p.get("current_price", 0.0)),
                    "unrealized_pl": float(p.get("unrealized_pl", 0.0)),
                    "unrealized_plpc": float(p.get("unrealized_plpc", 0.0)),
                }

            return {
                "ok": True,
                "equity": float(acc.get("equity", 0.0)),
                "last_equity": float(acc.get("last_equity", acc.get("equity", 0.0))),
                "cash": float(acc.get("cash", 0.0)),
                "buying_power": float(acc.get("buying_power", 0.0)),
                "positions": positions,
                "error": None,
            }
        except Exception as exc:
            logger.error("Errore o timeout recupero stato portafoglio Alpaca: %s", exc)
            return {"ok": False, "equity": 0.0, "last_equity": 0.0, "cash": 0.0,
                    "buying_power": 0.0, "positions": {}, "error": str(exc)}

    # ------------------------------------------------------------------ dati di mercato
    def get_watchlist_market_data(self, tickers: list[str] | None = None) -> dict[str, dict[str, float]]:
        """Prezzi correnti, variazione giornaliera e distanza dai massimi per qualsiasi lista di simboli."""
        raw = tickers or list(config.CORE_WATCHLIST)
        symbols = sorted({t.strip().upper() for t in raw if SYMBOL_RE.match(t.strip().upper())})
        result: dict[str, dict[str, float]] = {}
        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            try:
                resp = requests.get(
                    f"{self.data_url}/v2/stocks/snapshots",
                    params={"symbols": ",".join(chunk), "feed": "iex"},
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                snapshots = resp.json()
            except Exception as exc:
                logger.error("Errore o timeout recupero market data Alpaca: %s", exc)
                continue

            for sym in chunk:
                snap = snapshots.get(sym) or {}
                latest_trade = snap.get("latestTrade") or {}
                daily_bar = snap.get("dailyBar") or {}
                prev_bar = snap.get("prevDailyBar") or {}

                price = float(latest_trade.get("p") or daily_bar.get("c") or prev_bar.get("c") or 0.0)
                high = float(daily_bar.get("h") or prev_bar.get("h") or price)
                prev_close = float(prev_bar.get("c") or price)
                if price <= 0:
                    continue
                result[sym] = {
                    "price": round(price, 4 if price < 1 else 2),
                    "daily_change_pct": round(((price - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0, 2),
                    "pullback_from_high_pct": round(((price - high) / high * 100.0) if high > 0 else 0.0, 2),
                    "volume": int(daily_bar.get("v") or 0),
                }
        return result

    def get_market_movers(self, top: int = 10) -> dict[str, list[str]]:
        """Scopre dinamicamente nuove opportunità: top gainers, top losers e titoli più scambiati."""
        out: dict[str, list[str]] = {"gainers": [], "losers": [], "most_actives": []}
        try:
            resp = requests.get(
                f"{self.data_url}/v1beta1/screener/stocks/movers",
                params={"top": top},
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            out["gainers"] = [g["symbol"] for g in data.get("gainers", []) if SYMBOL_RE.match(g.get("symbol", ""))]
            out["losers"] = [g["symbol"] for g in data.get("losers", []) if SYMBOL_RE.match(g.get("symbol", ""))]
        except Exception as exc:
            logger.warning("Screener movers non disponibile: %s", exc)
        try:
            resp = requests.get(
                f"{self.data_url}/v1beta1/screener/stocks/most-actives",
                params={"by": "volume", "top": top},
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            out["most_actives"] = [
                a["symbol"] for a in data.get("most_actives", []) if SYMBOL_RE.match(a.get("symbol", ""))
            ]
        except Exception as exc:
            logger.warning("Screener most-actives non disponibile: %s", exc)
        return out

    def check_asset_tradable(self, symbol: str) -> tuple[bool, str]:
        """Verifica in tempo reale che il simbolo sia un'azione/ETF USA attiva, negoziabile e non OTC."""
        sym = symbol.strip().upper()
        if not SYMBOL_RE.match(sym):
            return False, f"Simbolo '{sym}' non valido."
        if sym in self._asset_cache:
            return self._asset_cache[sym]
        try:
            resp = requests.get(f"{self.base_url}/v2/assets/{sym}", headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 404:
                result = (False, f"'{sym}' non esiste su Alpaca.")
            else:
                resp.raise_for_status()
                a = resp.json()
                if not a.get("tradable"):
                    result = (False, f"'{sym}' non negoziabile su Alpaca.")
                elif a.get("status") != "active":
                    result = (False, f"'{sym}' non attivo.")
                elif str(a.get("exchange", "")).upper() == "OTC":
                    result = (False, f"'{sym}' è un titolo OTC (escluso).")
                elif a.get("class") != "us_equity":
                    result = (False, f"'{sym}' non è un'azione/ETF USA.")
                else:
                    result = (True, "")
        except Exception as exc:
            # Fail-closed: se non posso verificare, non compro
            return False, f"Verifica asset '{sym}' fallita: {exc}"
        self._asset_cache[sym] = result
        return result

    # ------------------------------------------------------------------ ordini
    def cancel_open_orders(self, symbol: str) -> int:
        """Cancella gli ordini aperti su un simbolo (es. gambe TP/SL di un bracket prima di una vendita manuale)."""
        config.verify_paper_endpoint(self.base_url)
        try:
            resp = requests.get(
                f"{self.base_url}/v2/orders",
                params={"status": "open", "symbols": symbol.upper(), "nested": "true"},
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            cancelled = 0
            for o in resp.json():
                requests.delete(f"{self.base_url}/v2/orders/{o['id']}", headers=self._headers(), timeout=self.timeout)
                cancelled += 1
            if cancelled:
                time.sleep(1.0)
            return cancelled
        except Exception as exc:
            logger.warning("Impossibile cancellare ordini aperti su %s: %s", symbol, exc)
            return 0

    def submit_order(
        self,
        ticker: str,
        side: str,
        qty: float,
        take_profit_price: float = 0.0,
        stop_loss_price: float = 0.0,
    ) -> dict[str, Any]:
        """
        Invia un ordine a mercato già validato da La Gabbia.
        Se BUY con TP e SL > 0 invia un ordine BRACKET (TP/SL eseguiti da Alpaca anche tra un ciclo e l'altro).
        """
        config.verify_paper_endpoint(self.base_url)
        ticker_clean = ticker.strip().upper()
        side_clean = side.strip().lower()
        if side_clean not in {"buy", "sell"}:
            return {"ok": False, "error": f"Side {side_clean} non autorizzato.", "symbol": ticker_clean}

        use_bracket = side_clean == "buy" and take_profit_price > 0 and stop_loss_price > 0
        qty_val = float(qty)
        if use_bracket:
            qty_val = float(math.floor(qty_val))
            if qty_val < 1:
                return {"ok": False, "error": "Ordine bracket richiede almeno 1 azione intera.", "symbol": ticker_clean}

        payload: dict[str, Any] = {
            "symbol": ticker_clean,
            "qty": str(int(qty_val)) if qty_val.is_integer() else str(round(qty_val, 4)),
            "side": side_clean,
            "type": "market",
            "time_in_force": "gtc" if use_bracket else "day",
        }
        if use_bracket:
            payload["order_class"] = "bracket"
            payload["take_profit"] = {"limit_price": str(_round_price(take_profit_price))}
            payload["stop_loss"] = {"stop_price": str(_round_price(stop_loss_price))}

        try:
            resp = requests.post(f"{self.base_url}/v2/orders", json=payload, headers=self._headers(), timeout=self.timeout)
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            return {
                "ok": True,
                "order_id": data.get("id"),
                "symbol": data.get("symbol", ticker_clean),
                "side": side_clean.upper(),
                "qty": qty_val,
                "bracket": use_bracket,
                "status": data.get("status", "accepted"),
                "error": None,
            }
        except Exception as exc:
            logger.error("Errore invio ordine su Alpaca (%s %s): %s", side_clean, ticker_clean, exc)
            return {"ok": False, "order_id": None, "symbol": ticker_clean, "side": side_clean.upper(),
                    "qty": qty_val, "status": "failed", "error": str(exc)}

    def submit_market_order(self, ticker: str, side: str, qty: float) -> dict[str, Any]:
        """Alias retro-compatibile (ordine a mercato semplice)."""
        return self.submit_order(ticker, side, qty)

    # ------------------------------------------------------------------ storico
    def get_recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recupera lo storico ordini (eseguiti, in attesa, cancellati) da Alpaca Paper."""
        config.verify_paper_endpoint(self.base_url)
        try:
            resp = requests.get(
                f"{self.base_url}/v2/orders",
                params={"status": "all", "limit": limit, "direction": "desc"},
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            orders: list[dict[str, Any]] = []
            for o in resp.json():
                qty_val = float(o.get("filled_qty") or o.get("qty") or 0.0)
                price_val = float(o.get("filled_avg_price") or 0.0)
                orders.append({
                    "id": o.get("id"),
                    "symbol": o.get("symbol"),
                    "side": str(o.get("side", "")).upper(),
                    "qty": float(o.get("qty") or 0.0),
                    "filled_qty": float(o.get("filled_qty") or 0.0),
                    "filled_avg_price": price_val,
                    "notional_value": round(qty_val * price_val, 2) if price_val > 0 else 0.0,
                    "type": o.get("type"),
                    "order_class": o.get("order_class"),
                    "status": str(o.get("status", "")).upper(),
                    "submitted_at": o.get("submitted_at"),
                    "filled_at": o.get("filled_at"),
                })
            return orders
        except Exception as exc:
            logger.error("Errore recupero storico ordini Alpaca: %s", exc)
            return []

    def get_fill_activities(self, max_pages: int = 5) -> list[dict[str, Any]]:
        """Recupera gli eseguiti (FILL) in ordine cronologico: base dati del motore di apprendimento."""
        config.verify_paper_endpoint(self.base_url)
        fills: list[dict[str, Any]] = []
        params: dict[str, Any] = {"direction": "desc", "page_size": 100}
        try:
            for _ in range(max_pages):
                resp = requests.get(
                    f"{self.base_url}/v2/account/activities/FILL",
                    params=params,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                fills.extend(batch)
                if len(batch) < 100:
                    break
                params["page_token"] = batch[-1]["id"]
        except Exception as exc:
            logger.error("Errore recupero fill Alpaca: %s", exc)
        return list(reversed(fills))

    def get_portfolio_history(self, period: str = "1M", timeframe: str = "1D") -> dict[str, Any]:
        """Recupera la curva storica dell'Equity dal conto Alpaca Paper."""
        config.verify_paper_endpoint(self.base_url)
        try:
            resp = requests.get(
                f"{self.base_url}/v2/account/portfolio/history",
                params={"period": period, "timeframe": timeframe},
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            points: list[dict[str, Any]] = []
            for ts, eq, pct in zip(data.get("timestamp") or [], data.get("equity") or [], data.get("profit_loss_pct") or []):
                if eq is not None and float(eq) > 0:
                    points.append({"timestamp": int(ts), "equity": round(float(eq), 2),
                                   "pnl_pct": round(float(pct or 0.0) * 100.0, 3)})
            return {"ok": True, "base_value": float(data.get("base_value") or 100000.0), "points": points}
        except Exception as exc:
            logger.error("Errore recupero portfolio history Alpaca: %s", exc)
            return {"ok": False, "base_value": 100000.0, "points": []}
