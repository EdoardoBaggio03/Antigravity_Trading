"""
AI Capital — Adaptive Edition.
Cicli di trading continui durante la seduta USA: Gemini decide quando entrare, su cosa e con quali parametri,
imparando dai risultati reali. La Gabbia applica solo i vincoli fissi di sicurezza.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import schedule

import config
from ai_trader import AITrader, DecisioneTradingAI
from broker_alpaca import AlpacaPaperBroker
from learning_engine import LearningEngine
from memory_manager import MemoryManager
from risk_cage import RiskCage
from telegram_notifier import TelegramNotifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("AICapital.Main")


def _daily_pnl(portfolio: dict[str, Any]) -> tuple[float, float]:
    equity = float(portfolio.get("equity") or 0.0)
    last = float(portfolio.get("last_equity") or equity or 100000.0)
    usd = equity - last
    return usd, (usd / last * 100.0) if last > 0 else 0.0


class AICapitalOrchestrator:
    def __init__(
        self,
        broker: AlpacaPaperBroker | None = None,
        cage: RiskCage | None = None,
        memory: MemoryManager | None = None,
        ai: AITrader | None = None,
        notifier: TelegramNotifier | None = None,
        learning: LearningEngine | None = None,
    ) -> None:
        config.verify_paper_endpoint()
        self.broker = broker or AlpacaPaperBroker()
        self.cage = cage or RiskCage(asset_validator=self.broker.check_asset_tradable)
        self.memory = memory or MemoryManager()
        self.ai = ai or AITrader()
        self.notifier = notifier or TelegramNotifier()
        self.learning = learning or LearningEngine()

    # ------------------------------------------------------------------ utilità
    @staticmethod
    def _now() -> datetime:
        return datetime.now(ZoneInfo(config.TIMEZONE))

    def _today_str(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def _learning_summary(self) -> dict[str, Any]:
        bilanci = self.memory.load_full_history().get("storico_bilanci", [])[-30:]
        return self.learning.prompt_summary(bilanci)

    def _collect_market(self, portfolio: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
        """Universo dinamico: posizioni + universo seme + watchlist scelta da Gemini + scanner di mercato."""
        state = self.memory.load_compact_state()
        movers = self.broker.get_market_movers(top=10)
        symbols = list(dict.fromkeys([
            *portfolio.get("positions", {}).keys(),
            *config.CORE_WATCHLIST,
            *state.get("watchlist_dinamica", []),
            *movers.get("gainers", []), *movers.get("losers", []), *movers.get("most_actives", []),
        ]))[:120]
        return self.broker.get_watchlist_market_data(symbols), movers

    def _ask_gemini(self, fase: str, portfolio: dict[str, Any], market: dict[str, Any],
                    movers: dict[str, list[str]]) -> DecisioneTradingAI | None:
        return self.ai.generate_decision(
            fase=fase,
            portfolio_summary=portfolio,
            market_snapshot=market,
            compact_memory_json=self.memory.get_prompt_payload(),
            learning_summary=self._learning_summary(),
            strategy_params=self.memory.get_strategy_params(),
            movers=movers,
            now_rome=self._now().strftime("%a %H:%M"),
        )

    def _apply_ai_parameters(self, decision: DecisioneTradingAI | None) -> dict[str, Any]:
        if decision is None:
            return self.memory.get_strategy_params()
        return self.memory.save_strategy_params(decision.parametri.model_dump(), decision.ticker_da_osservare or None)

    @staticmethod
    def _deterministic_exits(portfolio: dict[str, Any], params: dict[str, Any]) -> list[dict[str, Any]]:
        """Rete di sicurezza lato bot (il bracket lato broker è la prima difesa)."""
        _, day_pct = _daily_pnl(portfolio)
        target_hit = day_pct >= config.DAILY_NET_TARGET_PCT
        protect = target_hit and params.get("modalita_dopo_target") == "PROTEGGI_PROFITTI"
        tp, sl = float(params["take_profit_default_pct"]), float(params["stop_loss_default_pct"])

        exits: list[dict[str, Any]] = []
        for sym, pos in (portfolio.get("positions") or {}).items():
            qty = float(pos.get("qty") or 0.0)
            if qty <= 0:
                continue
            pl = float(pos.get("unrealized_plpc") or 0.0) * 100.0
            if pl >= tp or (protect and pl > 0.15):
                strat, why = "TAKE_PROFIT", f"Profitto {pl:+.2f}% (TP {tp:.2f}%{', target giornaliero protetto' if protect else ''})."
            elif pl <= -sl:
                strat, why = "STOP_LOSS", f"Perdita {pl:+.2f}% oltre lo stop di -{sl:.2f}%."
            else:
                continue
            exits.append({
                "ticker": sym, "azione": "SELL", "quantita": qty, "strategia": strat, "confidenza": 1.0,
                "razionale_tecnico": why,
                "perche_potrei_avere_torto": "Il prezzo potrebbe invertire subito dopo l'uscita.",
            })
        return exits

    @staticmethod
    def _attach_bracket(order: dict[str, Any], price: float, params: dict[str, Any]) -> dict[str, Any]:
        """Se Gemini non ha indicato TP/SL assoluti, li calcola dai suoi parametri di default."""
        if order.get("azione") != "BUY" or price <= 0:
            return order
        tp = float(order.get("take_profit_price") or 0.0)
        sl = float(order.get("stop_loss_price") or 0.0)
        if tp <= 0:
            tp = price * (1 + float(params["take_profit_default_pct"]) / 100.0)
        if sl <= 0:
            sl = price * (1 - float(params["stop_loss_default_pct"]) / 100.0)
        order["take_profit_price"] = round(tp, 2 if tp >= 1 else 4)
        order["stop_loss_price"] = round(sl, 2 if sl >= 1 else 4)
        order["quantita"] = float(int(float(order.get("quantita") or 0)))
        return order

    # ------------------------------------------------------------------ esecuzione
    def _execute(self, candidates: list[dict[str, Any]], portfolio: dict[str, Any],
                 prices: dict[str, float], params: dict[str, Any], label: str) -> dict[str, Any]:
        validation = self.cage.validate_orders_batch(
            orders=candidates,
            portfolio_equity=portfolio["equity"],
            available_cash=portfolio["cash"],
            current_positions=portfolio["positions"],
            market_prices=prices,
            endpoint_url=self.broker.base_url,
            max_weight=float(params["max_peso_singolo_asset_pct"]) / 100.0,
            max_positions=int(params["max_posizioni_aperte"]),
            min_cash_reserve_pct=float(params["cash_minimo_pct"]),
        )

        executed: list[dict[str, Any]] = []
        failed: list[str] = []
        for order in validation.approved_orders:
            sym, side, qty = order["ticker"], order["azione"], float(order["quantita"])
            if side == "SELL":
                self.broker.cancel_open_orders(sym)  # libera le azioni bloccate dalle gambe del bracket
                res = self.broker.submit_order(sym, "SELL", qty)
            else:
                res = self.broker.submit_order(sym, "BUY", qty,
                                               float(order.get("take_profit_price") or 0.0),
                                               float(order.get("stop_loss_price") or 0.0))
            if res.get("ok"):
                self.learning.register_order(res.get("order_id"), order)
                executed.append({
                    "ticker": sym, "azione": side, "quantita": qty,
                    "strategia": order.get("strategia", "ALTRO"),
                    "prezzo_stimato": prices.get(sym, 0.0),
                    "take_profit": order.get("take_profit_price"),
                    "stop_loss": order.get("stop_loss_price"),
                    "order_id": res.get("order_id"),
                })
            else:
                failed.append(f"{side} {sym}: {res.get('error')}")

        updated = self.broker.get_portfolio_state()
        self.memory.record_executed_orders(
            self._today_str(), executed,
            updated.get("positions", portfolio["positions"]) if updated.get("ok") else portfolio["positions"],
        )

        if executed or validation.rejected_orders or failed:
            _, day_pct = _daily_pnl(updated if updated.get("ok") else portfolio)
            lines = [f"🔔 [AI Capital - {label} | Oggi {day_pct:+.2f}% / target +{config.DAILY_NET_TARGET_PCT:.2f}%]"]
            for e in executed:
                extra = f" TP {e['take_profit']} / SL {e['stop_loss']}" if e["azione"] == "BUY" else ""
                lines.append(f"✅ {e['azione']} {e['quantita']:g} {e['ticker']} @~${e['prezzo_stimato']:.2f} [{e['strategia']}]{extra}")
            for r in validation.rejected_orders:
                if not r.reason.startswith("HOLD"):
                    lines.append(f"⛔ {r.order.get('azione')} {r.order.get('ticker')}: {r.reason}")
            for f in failed:
                lines.append(f"❌ {f}")
            self.notifier.send_message("\n".join(lines))

        return {"ok": True, "market_open": True, "executed": executed,
                "rejected": [r.reason for r in validation.rejected_orders], "failed": failed}

    # ------------------------------------------------------------------ sessioni
    def run_morning_session(self) -> dict[str, Any]:
        """Pre-market: aggiorna l'apprendimento, Gemini ricalibra parametri e watchlist, piano su Telegram."""
        logger.info("=== Sessione Pre-Market ===")
        portfolio = self.broker.get_portfolio_state()
        if not portfolio.get("ok"):
            self.notifier.send_message(f"⚠️ [AI Capital Pre-Market] Portafoglio non leggibile: {portfolio.get('error')}")
            return {"ok": False, "error": portfolio.get("error")}

        self.learning.rebuild(self.broker.get_fill_activities())
        market, movers = self._collect_market(portfolio)
        decision = self._ask_gemini("morning", portfolio, market, movers)
        if decision is None:
            self.notifier.send_message("⚠️ [AI Capital Pre-Market] Risposta AI non valida. Restano i parametri precedenti.")
            return {"ok": False, "error": "Invalid AI decision"}

        params = self._apply_ai_parameters(decision)
        self.memory.save_morning_plan(decision.model_dump())
        self.memory.mark_session_done(self._today_str(), "morning")

        idee = "\n".join(
            f"- {o.azione} {o.quantita:g} {o.ticker} [{o.strategia}, conf {o.confidenza:.0%}] {o.razionale_tecnico}"
            for o in decision.ordini if o.azione != "HOLD"
        ) or "Nessuna idea forte: attesa dei setup in seduta."
        self.notifier.send_message(
            f"🌅 [AI Capital - Piano | Target +{config.DAILY_NET_TARGET_PCT:.2f}%/gg]\n"
            f"Equity ${portfolio['equity']:,.2f} | Cash ${portfolio['cash']:,.2f}\n"
            f"Sentiment: {decision.sentiment_mercato}\n"
            f"Parametri scelti da Gemini: peso max {params['max_peso_singolo_asset_pct']}% | TP {params['take_profit_default_pct']}% | "
            f"SL {params['stop_loss_default_pct']}% | max pos {params['max_posizioni_aperte']} | dopo target: {params['modalita_dopo_target']}\n"
            f"Perché: {params['razionale_parametri']}\n"
            f"Watchlist: {', '.join(decision.ticker_da_osservare[:15]) or '-'}\n"
            f"Idee:\n{idee}"
        )
        return {"ok": True, "plan": decision.model_dump()}

    def run_trading_cycle(self, override_orders: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Ciclo intraday (ogni 15 min a mercato aperto): uscite di sicurezza + decisioni libere di Gemini."""
        clock = self.broker.is_market_open()
        if not clock.get("ok") or not clock.get("is_open"):
            reason = clock.get("error") or "Mercato USA chiuso (festività o fuori orario)"
            logger.info("Ciclo saltato: %s", reason)
            return {"ok": False, "market_open": False, "reason": reason, "executed": []}

        logger.info("=== Ciclo di Trading Intraday ===")
        portfolio = self.broker.get_portfolio_state()
        if not portfolio.get("ok"):
            return {"ok": False, "market_open": True, "reason": portfolio.get("error"), "executed": []}

        market, movers = self._collect_market(portfolio)
        prices = {s: float(i.get("price", 0.0)) for s, i in market.items()}
        for sym, pos in portfolio["positions"].items():
            prices.setdefault(sym, float(pos.get("current_price") or 0.0))

        if override_orders is not None:
            params = self.memory.get_strategy_params()
            candidates = list(override_orders)
        else:
            params = self.memory.get_strategy_params()
            exits = self._deterministic_exits(portfolio, params)
            decision = self._ask_gemini("intraday", portfolio, market, movers)
            params = self._apply_ai_parameters(decision)

            exit_syms = {o["ticker"] for o in exits}
            ai_orders = [o.model_dump() for o in decision.ordini
                         if o.azione != "HOLD" and o.ticker not in exit_syms] if decision else []

            _, day_pct = _daily_pnl(portfolio)
            if day_pct >= config.DAILY_NET_TARGET_PCT and params["modalita_dopo_target"] == "STOP_NUOVI_INGRESSI":
                ai_orders = [o for o in ai_orders if o["azione"] != "BUY"]
            candidates = exits + ai_orders

            # Prezzi per eventuali ticker proposti fuori dallo snapshot
            missing = [o["ticker"] for o in candidates if o["ticker"] not in prices]
            if missing:
                for s, i in self.broker.get_watchlist_market_data(missing).items():
                    prices[s] = float(i.get("price", 0.0))

        candidates = [self._attach_bracket(o, prices.get(o.get("ticker", ""), 0.0), params) for o in candidates]
        if not candidates:
            logger.info("Nessun ordine in questo ciclo (Gemini attende un setup migliore).")
            return {"ok": True, "market_open": True, "executed": [], "rejected": []}
        return self._execute(candidates, portfolio, prices, params, "Ciclo Intraday")

    # Alias retro-compatibili
    def run_open_execution(self, override_orders: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return self.run_trading_cycle(override_orders)

    def run_midday_check(self) -> dict[str, Any]:
        return self.run_trading_cycle()

    def run_close_report(self) -> dict[str, Any]:
        """Dopo la chiusura USA: ricostruisce i trade, Gemini estrae le lezioni e ricalibra i parametri."""
        logger.info("=== Sessione Serale (Bilancio & Apprendimento) ===")
        portfolio = self.broker.get_portfolio_state()
        if not portfolio.get("ok"):
            self.notifier.send_message(f"⚠️ [AI Capital Close] Bilancio impossibile: {portfolio.get('error')}")
            return {"ok": False, "error": portfolio.get("error")}

        stats = self.learning.rebuild(self.broker.get_fill_activities())
        market, movers = self._collect_market(portfolio)
        state = self.memory.load_compact_state()

        equity = float(portfolio["equity"])
        day_usd, day_pct = _daily_pnl(portfolio)
        target_hit = day_pct >= config.DAILY_NET_TARGET_PCT
        spy = market.get("SPY", {})
        spy_price, spy_day = float(spy.get("price", 0.0)), float(spy.get("daily_change_pct", 0.0))
        eq_init = float(state.get("equity_iniziale") or equity)
        spy_init = float(state.get("spy_prezzo_riferimento_iniziale") or spy_price or 1.0)
        cum_pf = (equity - eq_init) / eq_init * 100.0 if eq_init > 0 else 0.0
        cum_spy = (spy_price - spy_init) / spy_init * 100.0 if spy_init > 0 and spy_price > 0 else 0.0

        decision = self._ask_gemini("close", portfolio, market, movers)
        params = self._apply_ai_parameters(decision)
        nota = decision.nota_giorno_precedente if decision else f"Oggi {day_pct:+.2f}% vs target +{config.DAILY_NET_TARGET_PCT:.2f}%."
        regola = decision.nuova_regola_appresa if decision else ""

        pnl_report = {
            "equity_attuale": round(equity, 2), "cash_attuale": round(float(portfolio["cash"]), 2),
            "daily_pnl_usd": round(day_usd, 2), "daily_pnl_pct": round(day_pct, 2),
            "daily_target_pct": config.DAILY_NET_TARGET_PCT, "target_1pct_raggiunto": target_hit,
            "spy_prezzo_attuale": round(spy_price, 2), "spy_daily_pct": round(spy_day, 2),
            "cum_portfolio_pct": round(cum_pf, 2), "cum_spy_pct": round(cum_spy, 2),
            "alpha_vs_spy_pct": round(cum_pf - cum_spy, 2),
        }
        self.memory.update_end_of_day(self._today_str(), nota, regola, portfolio["positions"], pnl_report)
        self.memory.mark_session_done(self._today_str(), "close")

        tot = stats.get("totale", {})
        learn_txt = (f"Trade chiusi: {tot.get('n', 0)} | Win rate {tot.get('win_rate', 0)}% | "
                     f"Profit factor {tot.get('profit_factor', 0)} | P&L realizzato ${tot.get('pnl_usd', 0):,.2f}"
                     if tot.get("n") else "Nessun trade chiuso ancora: statistiche in costruzione.")
        self.notifier.send_message(
            f"🌙 [AI Capital - Bilancio | {'🎯 TARGET CENTRATO' if target_hit else '⏳ sotto target'}]\n"
            f"Equity ${equity:,.2f} | Oggi {day_pct:+.2f}% (${day_usd:+,.2f}) vs target +{config.DAILY_NET_TARGET_PCT:.2f}%\n"
            f"SPY oggi {spy_day:+.2f}% | Da inizio: {cum_pf:+.2f}% vs SPY {cum_spy:+.2f}%\n"
            f"📚 {learn_txt}\n"
            f"Lezione: {regola or '-'}\n"
            f"Parametri per domani: peso {params['max_peso_singolo_asset_pct']}% | TP {params['take_profit_default_pct']}% | "
            f"SL {params['stop_loss_default_pct']}% | dopo target: {params['modalita_dopo_target']}\n"
            f"Nota: {nota}"
        )
        return {"ok": True, "pnl_report": pnl_report}

    def run_auto(self) -> dict[str, Any] | None:
        """Decide da solo cosa fare: ciclo di trading a mercato aperto, piano al mattino, bilancio la sera."""
        now = self._now()
        clock = self.broker.is_market_open()
        if clock.get("ok") and clock.get("is_open"):
            return self.run_trading_cycle()
        if now.weekday() >= 5:
            logger.info("Weekend: nessuna azione.")
            return None
        today = self._today_str()
        minutes = now.hour * 60 + now.minute
        if 7 * 60 <= minutes < 15 * 60 + 30 and not self.memory.is_session_done(today, "morning"):
            return self.run_morning_session()
        if minutes >= 21 * 60 and not self.memory.is_session_done(today, "close"):
            return self.run_close_report()
        logger.info("Mercato chiuso e nessuna sessione da eseguire alle %s.", now.strftime("%H:%M"))
        return None


def start_scheduler(orchestrator: AICapitalOrchestrator, every_minutes: int = 15) -> None:
    schedule.every(every_minutes).minutes.do(orchestrator.run_auto)
    logger.info("Scheduler locale avviato: run_auto ogni %d minuti.", every_minutes)
    orchestrator.run_auto()
    while True:
        schedule.run_pending()
        time.sleep(15)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Capital - Adaptive Edition (+1% Daily Target)")
    parser.add_argument("--morning", action="store_true", help="Piano pre-market e ricalibrazione parametri")
    parser.add_argument("--cycle", action="store_true", help="Un ciclo di trading intraday")
    parser.add_argument("--open", action="store_true", help="Alias di --cycle")
    parser.add_argument("--midday", action="store_true", help="Alias di --cycle")
    parser.add_argument("--close", action="store_true", help="Bilancio serale e apprendimento")
    parser.add_argument("--auto", action="store_true", help="Sceglie da solo la sessione (usato da GitHub Actions)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    orchestrator = AICapitalOrchestrator()
    ran = False
    if args.morning:
        orchestrator.run_morning_session(); ran = True
    if args.cycle or args.open or args.midday:
        orchestrator.run_trading_cycle(); ran = True
    if args.close:
        orchestrator.run_close_report(); ran = True
    if args.auto:
        orchestrator.run_auto(); ran = True
    if not ran:
        start_scheduler(orchestrator)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
