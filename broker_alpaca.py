"""Client Alpaca Paper Trading con gestione resiliente di timeout, festività e snapshots."""

from __future__ import annotations

import logging
from typing import Any

import requests
import config

logger = logging.getLogger("AICapital.BrokerAlpaca")


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
        self.secret_key = (
            secret_key if secret_key is not None else config.ALPACA_SECRET_KEY
        )
        self.timeout = timeout
        self.data_url = "https://data.alpaca.markets"

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
        }

    def is_market_open(self) -> dict[str, Any]:
        """Verifica se il mercato USA è aperto (gestisce festività, weekend e timeout senza crash)."""
        config.verify_paper_endpoint(self.base_url)
        try:
            resp = requests.get(
                f"{self.base_url}/v2/clock",
                headers=self._headers(),
                timeout=self.timeout,
            )
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
            return {
                "ok": False,
                "is_open": False,
                "next_open": None,
                "next_close": None,
                "timestamp": None,
                "error": str(exc),
            }

    def get_portfolio_state(self) -> dict[str, Any]:
        """Recupera Equity, Cash e posizioni aperte su Alpaca Paper in modo resiliente."""
        config.verify_paper_endpoint(self.base_url)
        try:
            acc_resp = requests.get(
                f"{self.base_url}/v2/account",
                headers=self._headers(),
                timeout=self.timeout,
            )
            acc_resp.raise_for_status()
            acc = acc_resp.json()

            pos_resp = requests.get(
                f"{self.base_url}/v2/positions",
                headers=self._headers(),
                timeout=self.timeout,
            )
            pos_resp.raise_for_status()
            positions_raw = pos_resp.json()

            positions: dict[str, dict[str, float]] = {}
            for p in positions_raw:
                sym = str(p.get("symbol", "")).upper()
                positions[sym] = {
                    "qty": float(p.get("qty", 0.0)),
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
            return {
                "ok": False,
                "equity": 0.0,
                "last_equity": 0.0,
                "cash": 0.0,
                "buying_power": 0.0,
                "positions": {},
                "error": str(exc),
            }

    def get_watchlist_market_data(
        self, tickers: list[str] | None = None
    ) -> dict[str, dict[str, float]]:
        """Recupera prezzi correnti e variazione/distanza dai massimi per gli 8 ticker autorizzati."""
        symbols = sorted(tickers or config.ALLOWED_TICKERS)
        try:
            resp = requests.get(
                f"{self.data_url}/v2/stocks/snapshots",
                params={"symbols": ",".join(symbols), "feed": "iex"},
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            snapshots = resp.json()
            result: dict[str, dict[str, float]] = {}
            for sym in symbols:
                snap = snapshots.get(sym, {})
                latest_trade = snap.get("latestTrade") or {}
                daily_bar = snap.get("dailyBar") or {}
                prev_bar = snap.get("prevDailyBar") or {}

                price = float(
                    latest_trade.get("p")
                    or daily_bar.get("c")
                    or prev_bar.get("c")
                    or 0.0
                )
                high = float(daily_bar.get("h") or prev_bar.get("h") or price)
                prev_close = float(prev_bar.get("c") or price)
                change_pct = (
                    ((price - prev_close) / prev_close * 100.0)
                    if prev_close > 0
                    else 0.0
                )
                pullback_from_high_pct = (
                    ((price - high) / high * 100.0) if high > 0 else 0.0
                )

                if price > 0:
                    result[sym] = {
                        "price": round(price, 2),
                        "daily_change_pct": round(change_pct, 2),
                        "pullback_from_high_pct": round(pullback_from_high_pct, 2),
                    }
            return result
        except Exception as exc:
            logger.error("Errore o timeout recupero market data Alpaca: %s", exc)
            return {}

    def submit_market_order(
        self, ticker: str, side: str, qty: float
    ) -> dict[str, Any]:
        """Invia un ordine Market (day) su Alpaca Paper dopo che ha superato La Gabbia."""
        config.verify_paper_endpoint(self.base_url)
        ticker_clean = ticker.strip().upper()
        side_clean = side.strip().lower()
        if ticker_clean not in config.ALLOWED_TICKERS:
            return {
                "ok": False,
                "error": f"Ticker {ticker_clean} non autorizzato.",
            }
        if side_clean not in {"buy", "sell"}:
            return {
                "ok": False,
                "error": f"Side {side_clean} non autorizzato.",
            }

        payload = {
            "symbol": ticker_clean,
            "qty": str(round(qty, 4) if qty % 1 != 0 else int(qty)),
            "side": side_clean,
            "type": "market",
            "time_in_force": "day",
        }
        try:
            resp = requests.post(
                f"{self.base_url}/v2/orders",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "ok": True,
                "order_id": data.get("id"),
                "symbol": data.get("symbol", ticker_clean),
                "side": side_clean.upper(),
                "qty": qty,
                "status": data.get("status", "accepted"),
                "error": None,
            }
        except Exception as exc:
            logger.error("Errore invio ordine su Alpaca (%s %s): %s", side_clean, ticker_clean, exc)
            return {
                "ok": False,
                "order_id": None,
                "symbol": ticker_clean,
                "side": side_clean.upper(),
                "qty": qty,
                "status": "failed",
                "error": str(exc),
            }

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
            raw_orders = resp.json()
            orders: list[dict[str, Any]] = []
            for o in raw_orders:
                qty_val = float(o.get("filled_qty") or o.get("qty") or 0.0)
                price_val = float(o.get("filled_avg_price") or 0.0)
                orders.append(
                    {
                        "id": o.get("id"),
                        "symbol": o.get("symbol"),
                        "side": str(o.get("side", "")).upper(),
                        "qty": float(o.get("qty") or 0.0),
                        "filled_qty": float(o.get("filled_qty") or 0.0),
                        "filled_avg_price": price_val,
                        "notional_value": round(qty_val * price_val, 2)
                        if price_val > 0
                        else 0.0,
                        "type": o.get("type"),
                        "status": str(o.get("status", "")).upper(),
                        "submitted_at": o.get("submitted_at"),
                        "filled_at": o.get("filled_at"),
                    }
                )
            return orders
        except Exception as exc:
            logger.error("Errore recupero storico ordini Alpaca: %s", exc)
            return []

    def get_portfolio_history(
        self, period: str = "1M", timeframe: str = "1D"
    ) -> dict[str, Any]:
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
            timestamps = data.get("timestamp") or []
            equities = data.get("equity") or []
            pnl_pcts = data.get("profit_loss_pct") or []
            base_val = float(data.get("base_value") or 100000.0)

            points: list[dict[str, Any]] = []
            for ts, eq, pct in zip(timestamps, equities, pnl_pcts):
                if eq is not None and float(eq) > 0:
                    points.append(
                        {
                            "timestamp": int(ts),
                            "equity": round(float(eq), 2),
                            "pnl_pct": round(float(pct or 0.0) * 100.0, 3),
                        }
                    )
            return {"ok": True, "base_value": base_val, "points": points}
        except Exception as exc:
            logger.error("Errore recupero portfolio history Alpaca: %s", exc)
            return {"ok": False, "base_value": 100000.0, "points": []}

