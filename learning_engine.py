"""
Motore di Apprendimento di AI Capital.

Ricostruisce i trade chiusi dai FILL reali di Alpaca (abbinamento FIFO), li collega alla strategia
dichiarata da Gemini al momento dell'ingresso e calcola statistiche per strategia, ticker e fascia oraria.
Il riassunto compatto viene inserito nel prompt: Gemini "impara" leggendo i propri risultati reali
(apprendimento in-context, nessun riaddestramento del modello).
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger("AICapital.Learning")

_FRAC_RE = re.compile(r"\.(\d{6})\d+")


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(_FRAC_RE.sub(r".\1", ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _aggregate(trades: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    gross_win = sum(t["pnl_usd"] for t in wins)
    gross_loss = -sum(t["pnl_usd"] for t in losses)
    return {
        "n": n,
        "win_rate": round(len(wins) / n * 100.0, 1),
        "pnl_usd": round(sum(t["pnl_usd"] for t in trades), 2),
        "avg_pct": round(sum(t["pnl_pct"] for t in trades) / n, 2),
        "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0),
        "avg_holding_min": round(sum(t["holding_min"] for t in trades) / n, 1),
    }


class LearningEngine:
    MAX_ORDER_META = 800
    MAX_CLOSED_TRADES = 400

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.LEARNING_PATH

    # ------------------------------------------------------------------ persistenza
    def load(self) -> dict[str, Any]:
        default = {"aggiornato_il": None, "order_meta": {}, "closed_trades": [], "stats": {}}
        if not self.path.exists():
            return default
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("radice non valida")
            for k, v in default.items():
                data.setdefault(k, v)
            return data
        except Exception as exc:
            logger.warning("learning.json corrotto (%s): ripartenza pulita.", exc)
            return default

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def register_order(self, order_id: str | None, order: dict[str, Any]) -> None:
        """Memorizza strategia e confidenza dichiarate da Gemini per collegarle all'esito del trade."""
        if not order_id:
            return
        data = self.load()
        meta = data["order_meta"]
        meta[order_id] = {
            "ticker": order.get("ticker"),
            "azione": order.get("azione"),
            "strategia": order.get("strategia", "ALTRO"),
            "confidenza": order.get("confidenza"),
            "razionale": str(order.get("razionale_tecnico", ""))[:120],
            "ts": datetime.now(ZoneInfo(config.TIMEZONE)).isoformat(timespec="seconds"),
        }
        if len(meta) > self.MAX_ORDER_META:
            for key in list(meta.keys())[: len(meta) - self.MAX_ORDER_META]:
                meta.pop(key, None)
        self.save(data)

    # ------------------------------------------------------------------ ricostruzione
    def rebuild(self, fills: list[dict[str, Any]]) -> dict[str, Any]:
        """Abbina FIFO acquisti e vendite, calcola le statistiche e salva tutto in learning.json."""
        data = self.load()
        meta = data["order_meta"]
        tz = ZoneInfo(config.TIMEZONE)

        lots: dict[str, deque] = defaultdict(deque)
        merged: dict[tuple[str, str, str], dict[str, Any]] = {}

        for f in fills:
            sym = str(f.get("symbol", "")).upper()
            side = str(f.get("side", "")).lower()
            try:
                qty = float(f.get("qty") or 0.0)
                price = float(f.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            if not sym or qty <= 0 or price <= 0:
                continue
            ts = f.get("transaction_time")
            oid = f.get("order_id") or ""

            if side == "buy":
                lots[sym].append([qty, price, ts, oid])
                continue
            if not side.startswith("sell"):
                continue

            remaining = qty
            while remaining > 1e-9 and lots[sym]:
                lot = lots[sym][0]
                matched = min(remaining, lot[0])
                key = (sym, lot[3], oid)
                rec = merged.setdefault(key, {
                    "symbol": sym, "qty": 0.0, "entry_cost": 0.0, "exit_value": 0.0,
                    "entry_time": lot[2], "exit_time": ts, "entry_order": lot[3], "exit_order": oid,
                })
                rec["qty"] += matched
                rec["entry_cost"] += matched * lot[1]
                rec["exit_value"] += matched * price
                rec["exit_time"] = ts
                lot[0] -= matched
                remaining -= matched
                if lot[0] <= 1e-9:
                    lots[sym].popleft()

        closed: list[dict[str, Any]] = []
        for rec in merged.values():
            q = rec["qty"]
            if q <= 0:
                continue
            entry_px = rec["entry_cost"] / q
            exit_px = rec["exit_value"] / q
            t_in, t_out = _parse_ts(rec["entry_time"]), _parse_ts(rec["exit_time"])
            holding = (t_out - t_in).total_seconds() / 60.0 if t_in and t_out else 0.0
            em = meta.get(rec["entry_order"], {})
            closed.append({
                "symbol": rec["symbol"],
                "qty": round(q, 4),
                "entry_price": round(entry_px, 4),
                "exit_price": round(exit_px, 4),
                "pnl_usd": round(rec["exit_value"] - rec["entry_cost"], 2),
                "pnl_pct": round((exit_px - entry_px) / entry_px * 100.0, 2),
                "holding_min": round(holding, 1),
                "entry_time": rec["entry_time"],
                "exit_time": rec["exit_time"],
                "entry_hour_rome": t_in.astimezone(tz).hour if t_in else None,
                "strategia": em.get("strategia", "NON_CLASSIFICATA"),
                "confidenza": em.get("confidenza"),
            })
        closed.sort(key=lambda t: t.get("exit_time") or "")
        closed = closed[-self.MAX_CLOSED_TRADES:]

        data["closed_trades"] = closed
        data["stats"] = self.compute_stats(closed)
        data["aggiornato_il"] = datetime.now(tz).isoformat(timespec="seconds")
        self.save(data)
        return data["stats"]

    @staticmethod
    def compute_stats(closed: list[dict[str, Any]]) -> dict[str, Any]:
        by_strategy: dict[str, list] = defaultdict(list)
        by_ticker: dict[str, list] = defaultdict(list)
        by_hour: dict[str, list] = defaultdict(list)
        for t in closed:
            by_strategy[t["strategia"]].append(t)
            by_ticker[t["symbol"]].append(t)
            if t.get("entry_hour_rome") is not None:
                by_hour[f"{int(t['entry_hour_rome']):02d}"].append(t)
        return {
            "totale": _aggregate(closed),
            "per_strategia": {k: _aggregate(v) for k, v in by_strategy.items()},
            "per_ticker": {k: _aggregate(v) for k, v in by_ticker.items()},
            "per_ora_ingresso": {k: _aggregate(v) for k, v in sorted(by_hour.items())},
        }

    # ------------------------------------------------------------------ riassunto per il prompt
    def prompt_summary(self, daily_reports: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Riassunto ultra-compatto (poche centinaia di token) dei risultati passati."""
        data = self.load()
        stats = data.get("stats") or {}
        tot = stats.get("totale") or {"n": 0}
        summary: dict[str, Any] = {"trade_chiusi": tot.get("n", 0)}

        if tot.get("n"):
            summary["totale"] = {k: tot[k] for k in ("win_rate", "profit_factor", "avg_pct", "avg_win_pct",
                                                    "avg_loss_pct", "pnl_usd", "avg_holding_min")}
            summary["per_strategia"] = {
                k: [v["n"], v["win_rate"], v["avg_pct"], v["pnl_usd"]]
                for k, v in (stats.get("per_strategia") or {}).items()
            }
            tickers = sorted((stats.get("per_ticker") or {}).items(), key=lambda kv: kv[1]["pnl_usd"], reverse=True)
            summary["ticker_migliori"] = [[k, v["n"], v["win_rate"], v["pnl_usd"]] for k, v in tickers[:5]]
            summary["ticker_peggiori"] = [[k, v["n"], v["win_rate"], v["pnl_usd"]] for k, v in tickers[-5:] if v["pnl_usd"] < 0]
            hours = sorted((stats.get("per_ora_ingresso") or {}).items(), key=lambda kv: kv[1]["avg_pct"], reverse=True)
            summary["ore_ingresso_rome"] = [[k, v["n"], v["win_rate"], v["avg_pct"]] for k, v in hours]
            summary["ultimi_trade"] = [
                [t["symbol"], t["strategia"], t["pnl_pct"], t["holding_min"]]
                for t in (data.get("closed_trades") or [])[-8:]
            ]
            summary["legenda"] = "strategia/ticker/ore=[n,win%,avg%,pnl$]; ultimi=[sym,strat,pnl%,min]"

        if daily_reports:
            days = [r.get("pnl_report", {}) for r in daily_reports if r.get("pnl_report")]
            pcts = [float(d.get("daily_pnl_pct", 0.0)) for d in days]
            if pcts:
                target = config.DAILY_NET_TARGET_PCT
                summary["giorni"] = {
                    "n": len(pcts),
                    "target_centrato": sum(1 for p in pcts if p >= target),
                    "media_pct": round(sum(pcts) / len(pcts), 2),
                    "ultimi_5_pct": [round(p, 2) for p in pcts[-5:]],
                }
        return summary

    def dashboard_view(self) -> dict[str, Any]:
        data = self.load()
        return {
            "aggiornato_il": data.get("aggiornato_il"),
            "stats": data.get("stats") or {},
            "closed_trades": (data.get("closed_trades") or [])[-30:],
        }
