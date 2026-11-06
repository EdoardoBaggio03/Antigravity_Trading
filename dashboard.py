"""
Gestionale Web & Terminale Quantitativo per AI Capital.
- Modalità Live Server (default): `python dashboard.py` -> apre http://localhost:8080 con dati live Alpaca.
- Modalità Export Cloud (per GitHub Actions / GitHub Pages): `python dashboard.py --export` -> aggiorna docs/data.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import webbrowser
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import config
from broker_alpaca import AlpacaPaperBroker
from main import AICapitalOrchestrator
from memory_manager import MemoryManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("AICapital.Dashboard")

DOCS_DIR = config.BASE_DIR / "docs"
DATA_JSON_PATH = DOCS_DIR / "data.json"


def build_dashboard_payload() -> dict[str, Any]:
    """Raccoglie in tempo reale stato Alpaca Paper, Watchlist, Ordini, Curva Equity e Memoria AI."""
    broker = AlpacaPaperBroker()
    memory = MemoryManager()

    now_rome = datetime.now(ZoneInfo(config.TIMEZONE))
    clock = broker.is_market_open()
    portfolio_raw = broker.get_portfolio_state()
    watchlist = broker.get_watchlist_market_data()
    recent_orders = broker.get_recent_orders(limit=50)
    history_raw = broker.get_portfolio_history(period="1M", timeframe="1D")
    compact_mem = memory.load_compact_state()
    full_hist = memory.load_full_history()

    equity = float(portfolio_raw.get("equity") or 0.0)
    last_equity = float(portfolio_raw.get("last_equity") or equity or 100000.0)
    cash = float(portfolio_raw.get("cash") or 0.0)
    initial_equity = float(
        compact_mem.get("equity_iniziale")
        or history_raw.get("base_value")
        or 100000.0
    )

    positions_dict = portfolio_raw.get("positions") or {}
    positions_list: list[dict[str, Any]] = []
    invested_value = 0.0

    for sym, pos in sorted(positions_dict.items()):
        mv = float(pos.get("market_value") or 0.0)
        invested_value += mv
        weight_pct = (mv / equity * 100.0) if equity > 0 else 0.0
        positions_list.append(
            {
                "symbol": sym,
                "qty": round(float(pos.get("qty") or 0.0), 4),
                "avg_entry_price": round(float(pos.get("avg_entry_price") or 0.0), 4),
                "current_price": round(float(pos.get("current_price") or 0.0), 4),
                "market_value": round(mv, 2),
                "weight_pct": round(weight_pct, 2),
                "unrealized_pl": round(float(pos.get("unrealized_pl") or 0.0), 2),
                "unrealized_pl_pct": round(
                    float(pos.get("unrealized_plpc") or 0.0) * 100.0, 2
                ),
            }
        )

    daily_pnl = equity - last_equity if equity > 0 else 0.0
    daily_pnl_pct = (daily_pnl / last_equity * 100.0) if last_equity > 0 else 0.0
    total_pnl = equity - initial_equity if equity > 0 else 0.0
    total_pnl_pct = (total_pnl / initial_equity * 100.0) if initial_equity > 0 else 0.0

    # Costruzione curva Equity con data leggibile + punto live odierno
    chart_points: list[dict[str, Any]] = []
    for pt in history_raw.get("points", []):
        dt_str = datetime.fromtimestamp(
            pt["timestamp"], tz=ZoneInfo(config.TIMEZONE)
        ).strftime("%d/%m")
        chart_points.append(
            {
                "date": dt_str,
                "equity": pt["equity"],
                "pnl_pct": pt["pnl_pct"],
            }
        )

    today_label = now_rome.strftime("%d/%m (Live)")
    if equity > 0:
        if chart_points and chart_points[-1]["date"] == now_rome.strftime("%d/%m"):
            chart_points[-1] = {
                "date": today_label,
                "equity": round(equity, 2),
                "pnl_pct": round(total_pnl_pct, 3),
            }
        else:
            chart_points.append(
                {
                    "date": today_label,
                    "equity": round(equity, 2),
                    "pnl_pct": round(total_pnl_pct, 3),
                }
            )

    payload = {
        "generated_at": now_rome.strftime("%d/%m/%Y %H:%M:%S"),
        "timezone": config.TIMEZONE,
        "paper_endpoint": config.PAPER_ENDPOINT_LOCKED,
        "clock": clock,
        "portfolio": {
            "ok": portfolio_raw.get("ok", False),
            "equity": round(equity, 2),
            "last_equity": round(last_equity, 2),
            "initial_equity": round(initial_equity, 2),
            "cash": round(cash, 2),
            "invested_value": round(invested_value, 2),
            "daily_pnl": round(daily_pnl, 2),
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
        },
        "positions_list": positions_list,
        "watchlist": watchlist,
        "recent_orders": recent_orders,
        "equity_history": {
            "base_value": round(initial_equity, 2),
            "points": chart_points[-30:],
        },
        "compact_memory": compact_mem,
        "storico_bilanci": (full_hist.get("storico_bilanci") or [])[-15:],
    }
    return payload


def export_static_snapshot(output_path: Path | None = None) -> dict[str, Any]:
    """Esporta lo snapshot JSON aggiornato in `docs/data.json`."""
    target = output_path or DATA_JSON_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = build_dashboard_payload()
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("Snapshot gestionale salvato in %s", target)
    return payload


class DashboardHTTPRequestHandler(SimpleHTTPRequestHandler):
    """Server HTTP per il Gestionale Web AI Capital."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(DOCS_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        # Log silenzioso per richieste statiche
        pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/dashboard":
            try:
                payload = export_static_snapshot()
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                err = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(err)
            return

        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/run-session":
            qs = parse_qs(parsed.query)
            phase = (qs.get("phase") or ["morning"])[0].strip().lower()
            orchestrator = AICapitalOrchestrator()
            try:
                if phase == "morning":
                    res = orchestrator.run_morning_session()
                elif phase == "open":
                    res = orchestrator.run_open_execution()
                elif phase == "midday":
                    res = orchestrator.run_midday_check()
                elif phase == "close":
                    res = orchestrator.run_close_report()
                else:
                    res = {"ok": False, "error": f"Fase sconosciuta: {phase}"}

                export_static_snapshot()
                body = json.dumps(res, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                err = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(err)
            return

        self.send_response(404)
        self.end_headers()


def run_server(port: int = 8080, open_browser: bool = True) -> None:
    export_static_snapshot()
    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, DashboardHTTPRequestHandler)
    url = f"http://localhost:{port}"
    logger.info("==========================================================")
    logger.info("🚀 Gestionale Web AI Capital attivo su: %s", url)
    logger.info("   Premi CTRL+C nel terminale per chiudere il server.")
    logger.info("==========================================================")

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Chiusura Gestionale Web AI Capital.")
        httpd.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Capital - Gestionale Web & Dashboard")
    parser.add_argument(
        "--export",
        action="store_true",
        help="Aggiorna solo il file statico docs/data.json ed esce (usato da GitHub Actions)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Porta HTTP per il server locale (default: 8080)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Non aprire automaticamente il browser all'avvio",
    )
    args = parser.parse_args()

    if args.export:
        export_static_snapshot()
        return 0

    run_server(port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
