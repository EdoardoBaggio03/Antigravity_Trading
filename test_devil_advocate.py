"""
Stress Test Automatico ("L'Avvocato del Diavolo") per AI Capital.
Verifica a costo zero token API (100% Mock) i 6 scenari critici richiesti.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from ai_trader import AITrader
from broker_alpaca import AlpacaPaperBroker
from learning_engine import LearningEngine
from main import AICapitalOrchestrator
from memory_manager import MemoryManager
from risk_cage import RiskCage
from telegram_notifier import TelegramNotifier


class TestDevilAdvocate(unittest.TestCase):
    """Suite di stress test deterministica su tutti i 6 casi critici."""

    def setUp(self) -> None:
        self.cage = RiskCage(
            base_url="https://paper-api.alpaca.markets",
            max_single_asset_weight=0.20,
        )
        self.prices = {
            "SPY": 500.0,
            "QQQ": 400.0,
            "IWM": 200.0,
            "GLD": 200.0,
            "TLT": 100.0,
            "AAPL": 200.0,
            "MSFT": 400.0,
            "NVDA": 100.0,
            "TSLA": 250.0,
            "GME_UNLISTED": 25.0,
        }

    def test_1_forbidden_ticker_and_short_selling(self) -> None:
        """Caso 1: Titolo non negoziabile (simbolo invalido / OTC) o vendita allo scoperto (Short) + blocco endpoint Live."""
        # 1a. Verifica blocco immediato endpoint Live
        with self.assertRaises(RuntimeError):
            RiskCage(base_url="https://api.alpaca.markets")

        # 1b. Universo dinamico: un titolo OTC segnalato dal validatore Alpaca viene rifiutato
        otc_cage = RiskCage(
            base_url="https://paper-api.alpaca.markets",
            asset_validator=lambda s: (False, f"'{s}' è un titolo OTC (escluso).") if s == "XYZQ" else (True, ""),
        )
        otc_report = otc_cage.validate_orders_batch(
            orders=[{"ticker": "XYZQ", "azione": "BUY", "quantita": 10,
                     "perche_potrei_avere_torto": "Titolo illiquido, spread enormi."},
                    {"ticker": "SMCI", "azione": "BUY", "quantita": 10,
                     "perche_potrei_avere_torto": "Volatilità elevata post-trimestrale."}],
            portfolio_equity=10_000.0, available_cash=10_000.0, current_positions={},
            market_prices={"XYZQ": 3.0, "SMCI": 40.0},
        )
        self.assertEqual([o["ticker"] for o in otc_report.approved_orders], ["SMCI"])
        self.assertIn("OTC", otc_report.rejected_orders[0].reason)

        # 1c. Simbolo malformato, tentativo di Short e azione SHORT esplicita
        orders = [
            {
                "ticker": "GME_UNLISTED",
                "azione": "BUY",
                "quantita": 5,
                "acquisto_sui_massimi_senza_ritracciamento": False,
                "razionale_tecnico": "Breakout tecnico",
                "perche_potrei_avere_torto": "Volatilità estrema su meme stock non autorizzata.",
            },
            {
                "ticker": "NVDA",
                "azione": "SELL",
                "quantita": 10,  # Possedute: 0 -> tentativo di Short
                "acquisto_sui_massimi_senza_ritracciamento": False,
                "razionale_tecnico": "Presa di profitto",
                "perche_potrei_avere_torto": "Il trend rialzista AI potrebbe proseguire.",
            },
            {
                "ticker": "AAPL",
                "azione": "SHORT",
                "quantita": 5,
                "acquisto_sui_massimi_senza_ritracciamento": False,
                "razionale_tecnico": "Scommetto al ribasso",
                "perche_potrei_avere_torto": "Trimestrale sopra le attese.",
            },
        ]

        report = self.cage.validate_orders_batch(
            orders=orders,
            portfolio_equity=10_000.0,
            available_cash=10_000.0,
            current_positions={},
            market_prices=self.prices,
        )
        self.assertEqual(len(report.approved_orders), 0)
        self.assertEqual(len(report.rejected_orders), 3)

    def test_2_single_asset_concentration_18_to_21_percent(self) -> None:
        """Caso 2: Acquisto che porta un singolo asset dal 18% al 21% dell'Equity totale."""
        # Equity: $10,000. Già detenuto in AAPL: 9 azioni a $200 = $1,800 (18% dell'Equity).
        # Nuovo ordine BUY di 1.5 azioni AAPL ($300) -> porterebbe AAPL a $2,100 (21% > 20%).
        current_positions = {
            "AAPL": {"qty": 9.0, "market_value": 1_800.0},
        }
        orders = [
            {
                "ticker": "AAPL",
                "azione": "BUY",
                "quantita": 1.5,
                "acquisto_sui_massimi_senza_ritracciamento": False,
                "razionale_tecnico": "Incremento su ritracciamento media 20gg",
                "perche_potrei_avere_torto": "Possibile debolezza consumi iPhone in Cina.",
            }
        ]

        report = self.cage.validate_orders_batch(
            orders=orders,
            portfolio_equity=10_000.0,
            available_cash=8_200.0,
            current_positions=current_positions,
            market_prices=self.prices,
        )
        self.assertEqual(len(report.approved_orders), 0)
        self.assertEqual(len(report.rejected_orders), 1)
        self.assertIn("21.00%", report.rejected_orders[0].reason)

    def test_3_corrupted_json_or_empty_perche_potrei_avere_torto(self) -> None:
        """Caso 3: Risposta AI con JSON corrotto o campo 'perche_potrei_avere_torto' vuoto."""
        ai = AITrader(api_key="mock_key", client=MagicMock())

        # 3a. JSON corrotto/troncato dall'AI
        corrupted_decision = ai.parse_and_validate_ai_response("{not_valid_json: 123")
        self.assertIsNone(corrupted_decision)

        # 3b. JSON sintatticamente valido ma con 'perche_potrei_avere_torto' vuoto nello schema AI
        empty_torto_payload = {
            "fase_sessione": "open",
            "sentiment_mercato": "Mercato costruttivo su ritracciamento",
            "intervento_critico_necessario": False,
            "ordini": [
                {
                    "ticker": "MSFT",
                    "azione": "BUY",
                    "quantita": 2,
                    "acquisto_sui_massimi_senza_ritracciamento": False,
                    "razionale_tecnico": "Supporto statico testato",
                    "perche_potrei_avere_torto": "   ",
                }
            ],
            "nota_giorno_precedente": "Monitorare MSFT.",
            "nuova_regola_appresa": "",
        }
        invalid_decision = ai.parse_and_validate_ai_response(empty_torto_payload)
        self.assertIsNone(invalid_decision)

        # 3c. Verifica che anche La Gabbia blocchi direttamente un ordine con 'perche_potrei_avere_torto' vuoto
        cage_report = self.cage.validate_orders_batch(
            orders=empty_torto_payload["ordini"],
            portfolio_equity=10_000.0,
            available_cash=10_000.0,
            current_positions={},
            market_prices=self.prices,
        )
        self.assertEqual(len(cage_report.approved_orders), 0)
        self.assertEqual(len(cage_report.rejected_orders), 1)

    def test_4_missing_or_corrupted_memory_files_on_startup(self) -> None:
        """Caso 4: File di memoria assente o corrotto all'avvio."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storico_file = tmp_path / "storico_completo.json"
            compatto_file = tmp_path / "stato_compatto.json"

            mem = MemoryManager(storico_path=storico_file, compatto_path=compatto_file)

            # 4a. File assenti all'avvio -> creati automaticamente senza errori
            state_initial = mem.load_compact_state()
            self.assertIn("ultimi_5_giorni_operazioni", state_initial)
            self.assertTrue(compatto_file.exists())

            # 4b. Corruzione del file di stato compatto e storico completo
            compatto_file.write_text("<<< FILE CORROTTO NON JSON >>>", encoding="utf-8")
            storico_file.write_text("{corrotto", encoding="utf-8")

            recovered_state = mem.load_compact_state()
            recovered_history = mem.load_full_history()
            self.assertIsInstance(recovered_state, dict)
            self.assertIsInstance(recovered_history, dict)
            self.assertIn("nota_giorno_precedente", recovered_state)
            # Verifica che il payload per il prompt sia sempre ben sotto i 2.500 token
            prompt_payload = mem.get_prompt_payload()
            self.assertLess(len(prompt_payload), 4000)

    def test_5_api_timeout_and_us_market_closed_holiday(self) -> None:
        """Caso 5: Timeout API Alpaca/Telegram o mercato USA chiuso per festività."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            mem = MemoryManager(
                storico_path=tmp_path / "storico.json",
                compatto_path=tmp_path / "compatto.json",
            )
            broker = AlpacaPaperBroker(
                api_key="test",
                secret_key="test",
                base_url="https://paper-api.alpaca.markets",
            )
            notifier = TelegramNotifier(bot_token="fake_token", chat_id="12345")

            # 5a. Timeout di rete sia su Alpaca che su Telegram -> nessun crash
            with patch(
                "requests.get", side_effect=requests.exceptions.Timeout("Alpaca Timeout")
            ), patch(
                "requests.post",
                side_effect=requests.exceptions.Timeout("Telegram Timeout"),
            ):
                clock_status = broker.is_market_open()
                self.assertFalse(clock_status["ok"])
                self.assertFalse(clock_status["is_open"])

                tg_ok = notifier.send_message("Test messaggio in timeout")
                self.assertFalse(tg_ok)

            # 5b. Mercato USA chiuso per festività alle 15:35 -> nessun ordine inviato, nessun crash
            mock_broker = MagicMock(spec=AlpacaPaperBroker)
            mock_broker.base_url = "https://paper-api.alpaca.markets"
            mock_broker.is_market_open.return_value = {
                "ok": True,
                "is_open": False,
                "reason": "Thanksgiving Holiday",
            }
            mock_notifier = MagicMock(spec=TelegramNotifier)
            mock_ai = MagicMock(spec=AITrader)

            orchestrator = AICapitalOrchestrator(
                broker=mock_broker,
                cage=self.cage,
                memory=mem,
                ai=mock_ai,
                notifier=mock_notifier,
            )
            res = orchestrator.run_open_execution()
            self.assertFalse(res["ok"])
            self.assertFalse(res["market_open"])
            self.assertEqual(res["executed"], [])
            mock_broker.submit_market_order.assert_not_called()

    def test_6_multiple_orders_exceeding_available_cash(self) -> None:
        """Caso 6: Più ordini nella stessa sessione la cui somma supera il Cash disponibile."""
        # Equity: $10,000, ma Cash disponibile: solo $2,500 (il resto è già investito in altri asset).
        # Ordine 1: BUY 4 SPY @ $500 = $2,000 (20% Equity, coperto da $2,500 Cash) -> APPROVATO (Cash residuo: $500)
        # Ordine 2: BUY 4 QQQ @ $400 = $1,600 (16% Equity, ma richiede $1,600 > $500 Cash residuo) -> BLOCCATO
        # Ordine 3: BUY 2 GLD @ $200 = $400 (4% Equity, coperto dai $500 di Cash residuo) -> APPROVATO (Cash residuo: $100)
        orders = [
            {
                "ticker": "SPY",
                "azione": "BUY",
                "quantita": 4,
                "acquisto_sui_massimi_senza_ritracciamento": False,
                "razionale_tecnico": "Pullback su EMA 50",
                "perche_potrei_avere_torto": "Possibile dato inflazione USA superiore alle attese.",
            },
            {
                "ticker": "QQQ",
                "azione": "BUY",
                "quantita": 4,
                "acquisto_sui_massimi_senza_ritracciamento": False,
                "razionale_tecnico": "Rimbalzo settore tech",
                "perche_potrei_avere_torto": "Rialzo dei rendimenti decennali potrebbe comprimere i multipli.",
            },
            {
                "ticker": "GLD",
                "azione": "BUY",
                "quantita": 2,
                "acquisto_sui_massimi_senza_ritracciamento": False,
                "razionale_tecnico": "Copertura macro su ritracciamento",
                "perche_potrei_avere_torto": "Rafforzamento improvviso del dollaro USA.",
            },
        ]

        report = self.cage.validate_orders_batch(
            orders=orders,
            portfolio_equity=10_000.0,
            available_cash=2_500.0,
            current_positions={"TLT": {"qty": 75.0, "market_value": 7_500.0}},
            market_prices=self.prices,
        )

        approved_tickers = [o["ticker"] for o in report.approved_orders]
        rejected_tickers = [r.order["ticker"] for r in report.rejected_orders]

        self.assertEqual(approved_tickers, ["SPY", "GLD"])
        self.assertEqual(rejected_tickers, ["QQQ"])
        self.assertAlmostEqual(report.remaining_cash, 100.0)

    def test_7_learning_engine_rebuild_and_prompt_summary(self) -> None:
        """Caso 7: Motore di apprendimento FIFO: calcolo P&L per strategia/ticker/ora e summary compatto."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            learn_path = Path(tmp_dir) / "learning.json"
            engine = LearningEngine(path=learn_path)

            # Registra ordini di ingresso con strategia associata
            engine.register_order("order-in-1", {
                "ticker": "TQQQ",
                "azione": "BUY",
                "strategia": "MOMENTUM_BREAKOUT",
                "confidenza": 0.85,
                "razionale_tecnico": "Breakout volumetrico",
            })
            engine.register_order("order-in-2", {
                "ticker": "SQQQ",
                "azione": "BUY",
                "strategia": "INVERSE_HEDGE",
                "confidenza": 0.70,
                "razionale_tecnico": "Hedge ribasso",
            })

            # Mock di fill activities da Alpaca (1 trade vincente su TQQQ, 1 trade perdente su SQQQ)
            mock_fills = [
                # Buy TQQQ: 10 @ $50
                {"symbol": "TQQQ", "side": "buy", "qty": "10", "price": "50.0", "transaction_time": "2026-11-06T15:35:00.000000Z", "order_id": "order-in-1"},
                # Buy SQQQ: 20 @ $20
                {"symbol": "SQQQ", "side": "buy", "qty": "20", "price": "20.0", "transaction_time": "2026-11-06T16:00:00.000000Z", "order_id": "order-in-2"},
                # Sell TQQQ: 10 @ $52 (+$20 / +4%)
                {"symbol": "TQQQ", "side": "sell", "qty": "10", "price": "52.0", "transaction_time": "2026-11-06T16:45:00.000000Z", "order_id": "order-out-1"},
                # Sell SQQQ: 20 @ $19.50 (-$10 / -2.5%)
                {"symbol": "SQQQ", "side": "sell", "qty": "20", "price": "19.50", "transaction_time": "2026-11-06T17:10:00.000000Z", "order_id": "order-out-2"},
            ]

            stats = engine.rebuild(mock_fills)
            self.assertEqual(stats["totale"]["n"], 2)
            self.assertEqual(stats["totale"]["win_rate"], 50.0)
            self.assertAlmostEqual(stats["totale"]["pnl_usd"], 10.0)

            # Verifica attribuzione corretta per strategia
            strat_stats = stats["per_strategia"]
            self.assertEqual(strat_stats["MOMENTUM_BREAKOUT"]["win_rate"], 100.0)
            self.assertEqual(strat_stats["INVERSE_HEDGE"]["win_rate"], 0.0)

            # Verifica summary compatto per il prompt (< 1000 char)
            summary = engine.prompt_summary()
            self.assertEqual(summary["trade_chiusi"], 2)
            self.assertIn("MOMENTUM_BREAKOUT", summary["per_strategia"])

    def test_8_gemini_dynamic_parameters_and_hard_limit(self) -> None:
        """Caso 8: Parametri dinamici di Gemini (sanitizzazione e vincolo rigido max weight)."""
        import config

        # 8a. Parametri personalizzati scelti da Gemini entro limiti
        ai_custom_params = {
            "max_peso_singolo_asset_pct": 35.0,
            "take_profit_default_pct": 3.5,
            "stop_loss_default_pct": 1.2,
            "max_posizioni_aperte": 12,
            "cash_minimo_pct": 8.0,
            "modalita_dopo_target": "CONTINUA",
            "razionale_parametri": "Statistiche favorevoli sul momentum.",
        }
        sanitized = config.sanitize_strategy_params(ai_custom_params)
        self.assertEqual(sanitized["max_peso_singolo_asset_pct"], 35.0)
        self.assertEqual(sanitized["modalita_dopo_target"], "CONTINUA")

        # 8b. Allucinazione AI: tentativo di impostare max_peso = 90% -> bloccato da HARD_MAX_SINGLE_ASSET_WEIGHT (50%)
        hallucinated_params = {
            "max_peso_singolo_asset_pct": 90.0,
            "take_profit_default_pct": -5.0,  # Negativo -> corretto a soglia minima
            "modalita_dopo_target": "VALORE_NON_VALIDO",
        }
        clamped = config.sanitize_strategy_params(hallucinated_params)
        self.assertEqual(clamped["max_peso_singolo_asset_pct"], 50.0)
        self.assertEqual(clamped["take_profit_default_pct"], 0.2)
        self.assertEqual(clamped["modalita_dopo_target"], "CONTINUA")


if __name__ == "__main__":
    unittest.main(verbosity=2)
