"""Entrypoint CLI, Profit-Lock Engine (+1% Daily Target) e Scheduler (Europe/Rome) per AI Capital."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import schedule

import config
from ai_trader import AITrader
from broker_alpaca import AlpacaPaperBroker
from memory_manager import MemoryManager
from risk_cage import RiskCage
from telegram_notifier import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("AICapital.Main")


class AICapitalOrchestrator:
    """Coordina le sessioni giornaliere puntando al target netto del +1.00% al giorno."""

    def __init__(
        self,
        broker: AlpacaPaperBroker | None = None,
        cage: RiskCage | None = None,
        memory: MemoryManager | None = None,
        ai: AITrader | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        config.verify_paper_endpoint()
        self.broker = broker or AlpacaPaperBroker()
        self.cage = cage or RiskCage()
        self.memory = memory or MemoryManager()
        self.ai = ai or AITrader()
        self.notifier = notifier or TelegramNotifier()

    def _today_str(self) -> str:
        return datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")

    def _evaluate_auto_profit_and_stop_orders(
        self, portfolio: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        Regole deterministiche a supporto del Target +1.00% Netto Giornaliero:
        1. Portfolio Profit Lock: se il P&L giornaliero netto >= +1.00%, incassa tutte le posizioni in guadagno.
        2. Single-Position Take Profit: se una singola posizione guadagna >= +2.20%, incassa il profitto.
        3. Single-Position Stop Loss: se una singola posizione perde <= -1.50%, taglia subito la perdita.
        """
        equity = float(portfolio.get("equity") or 0.0)
        last_equity = float(portfolio.get("last_equity") or equity or 100000.0)
        daily_pnl_pct = (
            ((equity - last_equity) / last_equity * 100.0) if last_equity > 0 else 0.0
        )
        positions = portfolio.get("positions") or {}

        auto_orders: list[dict[str, Any]] = []
        target_locked = daily_pnl_pct >= config.DAILY_NET_TARGET_PCT

        for sym, pos in positions.items():
            qty = float(pos.get("qty") or 0.0)
            if qty <= 0:
                continue
            pl_pct = float(pos.get("unrealized_plpc") or 0.0) * 100.0

            if target_locked and pl_pct > 0.15:
                auto_orders.append(
                    {
                        "ticker": sym,
                        "azione": "SELL",
                        "quantita": qty,
                        "strategia": "TAKE_PROFIT_1PCT",
                        "acquisto_sui_massimi_senza_ritracciamento": False,
                        "razionale_tecnico": f"Target giornaliero +{config.DAILY_NET_TARGET_PCT:.2f}% raggiunto ({daily_pnl_pct:+.2f}%): Profit Lock su {sym} ({pl_pct:+.2f}%).",
                        "perche_potrei_avere_torto": "Il titolo potrebbe proseguire il rialzo intraday dopo la presa di profitto.",
                    }
                )
            elif pl_pct >= config.POSITION_TAKE_PROFIT_PCT:
                auto_orders.append(
                    {
                        "ticker": sym,
                        "azione": "SELL",
                        "quantita": qty,
                        "strategia": "TAKE_PROFIT_1PCT",
                        "acquisto_sui_massimi_senza_ritracciamento": False,
                        "razionale_tecnico": f"Take-Profit automatico su {sym} a {pl_pct:+.2f}% (soglia +{config.POSITION_TAKE_PROFIT_PCT:.2f}%).",
                        "perche_potrei_avere_torto": "Possibile continuazione del trend direzionale oltre il target.",
                    }
                )
            elif pl_pct <= config.POSITION_STOP_LOSS_PCT:
                auto_orders.append(
                    {
                        "ticker": sym,
                        "azione": "SELL",
                        "quantita": qty,
                        "strategia": "STOP_LOSS_DEFENSE",
                        "acquisto_sui_massimi_senza_ritracciamento": False,
                        "razionale_tecnico": f"Stop-Loss protettivo su {sym} a {pl_pct:+.2f}% (limite {config.POSITION_STOP_LOSS_PCT:.2f}%) per difendere il target giornaliero.",
                        "perche_potrei_avere_torto": "Possibile rimbalzo tecnico dopo aver toccato lo stop intraday.",
                    }
                )
        return auto_orders

    def run_morning_session(self) -> dict[str, Any]:
        """09:00 Europe/Rome: Scansione 30 strumenti, lettura memoria compatta e invio Piano +1% su Telegram."""
        logger.info("=== Avvio Sessione 09:00 (Pre-Market & Piano +1%% Daily Target) ===")
        portfolio = self.broker.get_portfolio_state()
        if not portfolio.get("ok"):
            msg = f"⚠️ [AI Capital 09:00] Impossibile leggere il portafoglio Alpaca: {portfolio.get('error')}"
            self.notifier.send_message(msg)
            return {"ok": False, "error": portfolio.get("error")}

        market_data = self.broker.get_watchlist_market_data()
        compact_mem = self.memory.get_prompt_payload()
        decision = self.ai.generate_decision(
            fase="morning",
            portfolio_summary=portfolio,
            market_snapshot=market_data,
            compact_memory_json=compact_mem,
        )

        if decision is None:
            msg = "⚠️ [AI Capital 09:00] Risposta AI non valida o assente. Nessun piano salvato."
            self.notifier.send_message(msg)
            return {"ok": False, "error": "Invalid AI decision"}

        plan_dict = decision.model_dump()
        self.memory.save_morning_plan(plan_dict)

        ordini_txt = (
            "\n".join(
                f"- {o.azione} {o.quantita} {o.ticker} [{o.strategia}] | {o.razionale_tecnico} | Rischio: {o.perche_potrei_avere_torto}"
                for o in decision.ordini
                if o.azione != "HOLD"
            )
            or "Nessuna operazione pianificata (HOLD)."
        )
        report = (
            f"🌅 [AI Capital - Piano 09:00 | Target +{config.DAILY_NET_TARGET_PCT:.2f}%/gg]\n"
            f"Equity: ${portfolio['equity']:.2f} | Cash: ${portfolio['cash']:.2f}\n"
            f"Universo: {len(config.ALLOWED_TICKERS)} Strumenti Multi-Asset\n"
            f"Sentiment: {decision.sentiment_mercato}\n"
            f"Ordini proposti:\n{ordini_txt}"
        )
        self.notifier.send_message(report)
        return {"ok": True, "plan": plan_dict}

    def run_open_execution(
        self, override_orders: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """15:35 Europe/Rome: Verifica apertura USA, Profit-Lock/Stop-Loss, validazione La Gabbia ed esecuzione."""
        logger.info("=== Avvio Sessione Esecutiva (Target +1%% Giornaliero) ===")
        clock = self.broker.is_market_open()
        if not clock.get("ok") or not clock.get("is_open"):
            reason = clock.get("error") or "Mercato USA chiuso (festività o fuori orario)"
            msg = f"⏸️ [AI Capital] Nessuna esecuzione: {reason}."
            logger.info(msg)
            self.notifier.send_message(msg)
            return {"ok": False, "market_open": False, "reason": reason, "executed": []}

        portfolio = self.broker.get_portfolio_state()
        if not portfolio.get("ok"):
            msg = f"⚠️ [AI Capital] Errore lettura portafoglio: {portfolio.get('error')}"
            self.notifier.send_message(msg)
            return {"ok": False, "market_open": True, "reason": portfolio.get("error"), "executed": []}

        market_data = self.broker.get_watchlist_market_data()
        prices = {sym: float(info.get("price", 0.0)) for sym, info in market_data.items()}

        if override_orders is not None:
            candidate_orders = override_orders
        else:
            # 1. Ordini automatici di Profit-Lock (+1% giornaliero / +2.2% posizione) e Stop-Loss (-1.5%)
            auto_orders = self._evaluate_auto_profit_and_stop_orders(portfolio)
            auto_tickers = {o["ticker"] for o in auto_orders}

            # 2. Decisione AI sui 30 strumenti
            compact_mem = self.memory.get_prompt_payload()
            decision = self.ai.generate_decision(
                fase="open",
                portfolio_summary=portfolio,
                market_snapshot=market_data,
                compact_memory_json=compact_mem,
            )
            if decision is None and not auto_orders:
                msg = "⚠️ [AI Capital] Decisione AI non valida/corrotta. Ordini bloccati."
                self.notifier.send_message(msg)
                return {"ok": False, "market_open": True, "reason": "AI response invalid", "executed": []}

            ai_orders = (
                [
                    o.model_dump()
                    for o in decision.ordini
                    if o.azione != "HOLD" and o.ticker not in auto_tickers
                ]
                if decision
                else []
            )
            candidate_orders = auto_orders + ai_orders

        validation = self.cage.validate_orders_batch(
            orders=candidate_orders,
            portfolio_equity=portfolio["equity"],
            available_cash=portfolio["cash"],
            current_positions=portfolio["positions"],
            market_prices=prices,
            endpoint_url=self.broker.base_url,
        )

        executed: list[dict[str, Any]] = []
        for order in validation.approved_orders:
            res = self.broker.submit_market_order(
                ticker=order["ticker"],
                side=order["azione"],
                qty=float(order["quantita"]),
            )
            if res.get("ok"):
                executed.append(
                    {
                        "ticker": order["ticker"],
                        "azione": order["azione"],
                        "quantita": order["quantita"],
                        "strategia": order.get("strategia", "TACTICAL"),
                        "prezzo_stimato": prices.get(order["ticker"], 0.0),
                        "order_id": res.get("order_id"),
                    }
                )

        updated_portfolio = self.broker.get_portfolio_state()
        final_positions = (
            updated_portfolio.get("positions", portfolio["positions"])
            if updated_portfolio.get("ok")
            else portfolio["positions"]
        )
        self.memory.record_executed_orders(
            date_str=self._today_str(),
            executed_orders=executed,
            current_positions=final_positions,
        )

        equity_now = float(portfolio["equity"])
        last_eq = float(portfolio.get("last_equity") or equity_now or 100000.0)
        day_pct = ((equity_now - last_eq) / last_eq * 100.0) if last_eq > 0 else 0.0

        rejected_txt = (
            "\n".join(
                f"⛔ {r.order.get('azione')} {r.order.get('ticker')}: {r.reason}"
                for r in validation.rejected_orders
            )
            or "Nessuno"
        )
        exec_txt = (
            "\n".join(
                f"✅ {e['azione']} {e['quantita']} {e['ticker']} (@ ~${e['prezzo_stimato']:.2f})"
                for e in executed
            )
            or "Nessun ordine eseguito"
        )
        msg = (
            f"🔔 [AI Capital - Esecuzione Mercato | Oggi: {day_pct:+.2f}% / Target +{config.DAILY_NET_TARGET_PCT:.2f}%]\n"
            f"Eseguiti:\n{exec_txt}\n"
            f"Bloccati da La Gabbia:\n{rejected_txt}"
        )
        self.notifier.send_message(msg)
        return {
            "ok": True,
            "market_open": True,
            "executed": executed,
            "rejected": [r.reason for r in validation.rejected_orders],
        }

    def run_midday_check(self) -> dict[str, Any]:
        """Check Intraday (17:00 / 18:30 / 20:30): esegue Profit-Lock (+1%), Stop-Loss o rotazioni tattiche."""
        logger.info("=== Avvio Check Intraday & Profit-Lock (+1%% Daily Target) ===")
        clock = self.broker.is_market_open()
        if not clock.get("ok") or not clock.get("is_open"):
            return {"ok": False, "market_open": False, "reason": "Mercato chiuso"}

        portfolio = self.broker.get_portfolio_state()
        if not portfolio.get("ok"):
            return {"ok": False, "error": portfolio.get("error")}

        auto_orders = self._evaluate_auto_profit_and_stop_orders(portfolio)
        market_data = self.broker.get_watchlist_market_data()
        compact_mem = self.memory.get_prompt_payload()
        decision = self.ai.generate_decision(
            fase="midday",
            portfolio_summary=portfolio,
            market_snapshot=market_data,
            compact_memory_json=compact_mem,
        )

        if decision is None and not auto_orders:
            return {"ok": False, "error": "Invalid AI decision"}

        ai_orders = (
            [o.model_dump() for o in decision.ordini if o.azione != "HOLD"]
            if (decision and (decision.intervento_critico_necessario or decision.ordini))
            else []
        )
        auto_tickers = {o["ticker"] for o in auto_orders}
        combined_orders = auto_orders + [
            o for o in ai_orders if o["ticker"] not in auto_tickers
        ]

        if not combined_orders:
            equity = float(portfolio["equity"])
            last_eq = float(portfolio.get("last_equity") or equity or 100000.0)
            day_pct = ((equity - last_eq) / last_eq * 100.0) if last_eq > 0 else 0.0
            sentiment = decision.sentiment_mercato if decision else "Posizioni monitorate"
            msg = (
                f"🛡️ [AI Capital Intraday] P&L Oggi: {day_pct:+.2f}% (Target +{config.DAILY_NET_TARGET_PCT:.2f}%). "
                f"Nessuna rotazione necessaria. ({sentiment})"
            )
            self.notifier.send_message(msg)
            return {"ok": True, "critical_action": False}

        return self.run_open_execution(override_orders=combined_orders)

    def run_close_report(self) -> dict[str, Any]:
        """21:05/21:50 Europe/Rome: Bilancio P&L vs Target +1.00% e SPY, aggiornamento memoria e report Telegram."""
        logger.info("=== Avvio Sessione Serale (Bilancio Target +1%% & Memoria) ===")
        portfolio = self.broker.get_portfolio_state()
        if not portfolio.get("ok"):
            msg = f"⚠️ [AI Capital Close] Impossibile generare bilancio: {portfolio.get('error')}"
            self.notifier.send_message(msg)
            return {"ok": False, "error": portfolio.get("error")}

        market_data = self.broker.get_watchlist_market_data()
        compact_state = self.memory.load_compact_state()

        equity = float(portfolio["equity"])
        last_equity = float(portfolio.get("last_equity") or equity)
        daily_pnl_usd = equity - last_equity
        daily_pnl_pct = (
            (daily_pnl_usd / last_equity * 100.0) if last_equity > 0 else 0.0
        )
        target_hit = daily_pnl_pct >= config.DAILY_NET_TARGET_PCT

        spy_info = market_data.get("SPY", {})
        spy_price = float(spy_info.get("price", 0.0))
        spy_daily_pct = float(spy_info.get("daily_change_pct", 0.0))

        eq_init = float(compact_state.get("equity_iniziale") or equity)
        spy_init = float(
            compact_state.get("spy_prezzo_riferimento_iniziale") or spy_price or 1.0
        )

        cum_portfolio_pct = (
            ((equity - eq_init) / eq_init * 100.0) if eq_init > 0 else 0.0
        )
        cum_spy_pct = (
            ((spy_price - spy_init) / spy_init * 100.0)
            if spy_init > 0 and spy_price > 0
            else 0.0
        )
        alpha_vs_spy = cum_portfolio_pct - cum_spy_pct

        decision = self.ai.generate_decision(
            fase="close",
            portfolio_summary=portfolio,
            market_snapshot=market_data,
            compact_memory_json=self.memory.get_prompt_payload(),
        )

        nota_domani = (
            decision.nota_giorno_precedente
            if decision
            else f"Chiusura con Equity ${equity:.2f} (Oggi {daily_pnl_pct:+.2f}% vs Target +{config.DAILY_NET_TARGET_PCT:.2f}%)."
        )
        nuova_regola = decision.nuova_regola_appresa if decision else ""

        pnl_report = {
            "equity_attuale": round(equity, 2),
            "cash_attuale": round(float(portfolio["cash"]), 2),
            "daily_pnl_usd": round(daily_pnl_usd, 2),
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "daily_target_pct": config.DAILY_NET_TARGET_PCT,
            "target_1pct_raggiunto": target_hit,
            "spy_prezzo_attuale": round(spy_price, 2),
            "spy_daily_pct": round(spy_daily_pct, 2),
            "cum_portfolio_pct": round(cum_portfolio_pct, 2),
            "cum_spy_pct": round(cum_spy_pct, 2),
            "alpha_vs_spy_pct": round(alpha_vs_spy, 2),
        }

        self.memory.update_end_of_day(
            date_str=self._today_str(),
            nota_domani=nota_domani,
            nuova_regola=nuova_regola,
            current_positions=portfolio["positions"],
            pnl_report=pnl_report,
        )

        badge_target = "🎯 TARGET +1% CENTRATO!" if target_hit else "⏳ In corsa per il target"
        report_msg = (
            f"🌙 [AI Capital - Bilancio Serale | {badge_target}]\n"
            f"Equity: ${equity:.2f} | Cash: ${portfolio['cash']:.2f}\n"
            f"Oggi Netto: {daily_pnl_pct:+.2f}% (${daily_pnl_usd:+.2f}) vs Target +{config.DAILY_NET_TARGET_PCT:.2f}%\n"
            f"Benchmark SPY Oggi: {spy_daily_pct:+.2f}%\n"
            f"Da Inizio: Portafoglio {cum_portfolio_pct:+.2f}% vs SPY {cum_spy_pct:+.2f}% (Alpha: {alpha_vs_spy:+.2f}%)\n"
            f"Nota per domani: {nota_domani}"
        )
        self.notifier.send_message(report_msg)
        return {"ok": True, "pnl_report": pnl_report}


def start_scheduler(orchestrator: AICapitalOrchestrator) -> None:
    """Avvia lo scheduler continuo sul fuso orario Europe/Rome con check intraday potenziati."""
    tz = config.TIMEZONE
    schedule.every().day.at("09:00", tz).do(orchestrator.run_morning_session)
    schedule.every().day.at("15:35", tz).do(orchestrator.run_open_execution)
    schedule.every().day.at("17:00", tz).do(orchestrator.run_midday_check)
    schedule.every().day.at("18:30", tz).do(orchestrator.run_midday_check)
    schedule.every().day.at("20:30", tz).do(orchestrator.run_midday_check)
    schedule.every().day.at("21:05", tz).do(orchestrator.run_close_report)

    logger.info(
        "Scheduler AI Capital (+1%% Daily Target) avviato (%s): 09:00 | 15:35 | 17:00 | 18:30 | 20:30 | 21:05",
        tz,
    )
    while True:
        schedule.run_pending()
        time.sleep(20)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI Capital - Agente Quantitativo Multi-Asset (+1% Daily Target)"
    )
    parser.add_argument(
        "--morning",
        action="store_true",
        help="Esegue subito la routine delle 09:00 (Analisi 30 asset e Piano Telegram)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Esegue subito la routine delle 15:35 (Validazione La Gabbia ed Esecuzione)",
    )
    parser.add_argument(
        "--midday",
        action="store_true",
        help="Esegue subito il check intraday (Profit-Lock +1%, Stop-Loss e Rotazione)",
    )
    parser.add_argument(
        "--close",
        action="store_true",
        help="Esegue subito la routine serale (Bilancio P&L vs Target +1% e SPY)",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Rileva automaticamente la fascia oraria Europe/Rome (per GitHub Actions cron)",
    )
    return parser.parse_args(argv)


def run_auto_by_rome_time(orchestrator: AICapitalOrchestrator) -> None:
    """Seleziona la routine corretta in base all'ora corrente in Europe/Rome."""
    now_rome = datetime.now(ZoneInfo(config.TIMEZONE))
    minutes_of_day = now_rome.hour * 60 + now_rome.minute
    logger.info(
        "Avvio --auto: orario rilevato Europe/Rome = %s",
        now_rome.strftime("%Y-%m-%d %H:%M:%S"),
    )
    if 7 * 60 <= minutes_of_day <= 12 * 60 + 30:
        orchestrator.run_morning_session()
    elif 14 * 60 + 30 <= minutes_of_day <= 16 * 60 + 25:
        orchestrator.run_open_execution()
    elif 16 * 60 + 26 <= minutes_of_day <= 20 * 60 + 50:
        orchestrator.run_midday_check()
    elif 20 * 60 + 51 <= minutes_of_day <= 23 * 60 + 30:
        orchestrator.run_close_report()
    else:
        logger.info(
            "Orario %s fuori dalle finestre operative (nessuna azione eseguita).",
            now_rome.strftime("%H:%M"),
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    orchestrator = AICapitalOrchestrator()

    ran_single = False
    if args.morning:
        orchestrator.run_morning_session()
        ran_single = True
    if args.open:
        orchestrator.run_open_execution()
        ran_single = True
    if args.midday:
        orchestrator.run_midday_check()
        ran_single = True
    if args.close:
        orchestrator.run_close_report()
        ran_single = True
    if args.auto:
        run_auto_by_rome_time(orchestrator)
        ran_single = True

    if not ran_single:
        start_scheduler(orchestrator)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
