"""Modulo di gestione della Memoria Compatta Anti-Sprechi per AI Capital."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger("AICapital.Memory")


def _now_iso() -> str:
    return datetime.now(ZoneInfo(config.TIMEZONE)).isoformat(timespec="seconds")


def _default_stato_compatto() -> dict[str, Any]:
    return {
        "ultimo_aggiornamento": _now_iso(),
        "spy_prezzo_riferimento_iniziale": None,
        "equity_iniziale": None,
        "nota_giorno_precedente": "Giorno 0: Portafoglio appena inizializzato. Osservare i livelli chiave prima di allocare.",
        "regole_apprese": [
            "Non inseguire mai rialzi estesi senza ritracciamento.",
            "Mantenere liquidità strategica per assorbire la volatilità.",
        ],
        "piano_mattutino_corrente": {},
        "posizioni_attuali": {},
        "ultimi_5_giorni_operazioni": [],
    }


def _default_storico_completo() -> dict[str, Any]:
    return {
        "creato_il": _now_iso(),
        "sessioni_giornaliere": [],
        "tutte_le_operazioni": [],
        "storico_bilanci": [],
    }


class MemoryManager:
    """Gestisce `storico_completo.json` e `stato_compatto.json` (< 2.500 token per prompt)."""

    MAX_REGOLE_APPRESE = 8
    MAX_GIORNI_OPERAZIONI_COMPATTE = 5

    def __init__(
        self,
        storico_path: Path | None = None,
        compatto_path: Path | None = None,
    ) -> None:
        self.storico_path = storico_path or config.STORICO_COMPLETO_PATH
        self.compatto_path = compatto_path or config.STATO_COMPATTO_PATH
        self.storico_path.parent.mkdir(parents=True, exist_ok=True)
        self.compatto_path.parent.mkdir(parents=True, exist_ok=True)

    def _safe_load_json(
        self, path: Path, default_factory: callable
    ) -> dict[str, Any]:
        """Carica un file JSON in modo resiliente. Se assente o corrotto, esegue backup e ricrea."""
        if not path.exists():
            logger.info("File memoria %s non trovato. Inizializzazione default.", path.name)
            data = default_factory()
            self._atomic_write_json(path, data)
            return data

        try:
            raw = path.read_text(encoding="utf-8").strip()
            if not raw:
                raise ValueError("File JSON vuoto")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("La radice del JSON deve essere un oggetto (dict)")
            return parsed
        except Exception as exc:
            backup_path = path.with_suffix(f".corrupted_{int(datetime.now().timestamp())}.bak")
            try:
                shutil.copy2(path, backup_path)
            except OSError:
                pass
            logger.warning(
                "File memoria %s corrotto (%s). Backup salvato in %s e ripristino stato pulito.",
                path.name,
                exc,
                backup_path.name,
            )
            data = default_factory()
            self._atomic_write_json(path, data)
            return data

    def _atomic_write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp_path.replace(path)

    def load_compact_state(self) -> dict[str, Any]:
        """Restituisce lo stato compatto validato e pronto per essere passato al prompt AI."""
        state = self._safe_load_json(self.compatto_path, _default_stato_compatto)
        defaults = _default_stato_compatto()
        for k, v in defaults.items():
            if k not in state:
                state[k] = v
        # Tronca preventivamente per garantire sempre < 2.500 token
        state["regole_apprese"] = list(state.get("regole_apprese", []))[
            -self.MAX_REGOLE_APPRESE :
        ]
        state["ultimi_5_giorni_operazioni"] = list(
            state.get("ultimi_5_giorni_operazioni", [])
        )[-self.MAX_GIORNI_OPERAZIONI_COMPATTE :]
        state["nota_giorno_precedente"] = str(
            state.get("nota_giorno_precedente", "")
        )[:600]
        return state

    def load_full_history(self) -> dict[str, Any]:
        """Restituisce l'intero storico archiviato."""
        history = self._safe_load_json(self.storico_path, _default_storico_completo)
        defaults = _default_storico_completo()
        for k, v in defaults.items():
            if k not in history:
                history[k] = v
        return history

    def save_morning_plan(self, plan_data: dict[str, Any]) -> None:
        """Salva il piano delle 09:00 nello stato compatto e nello storico completo."""
        state = self.load_compact_state()
        state["ultimo_aggiornamento"] = _now_iso()
        state["piano_mattutino_corrente"] = {
            "timestamp": _now_iso(),
            "sentiment_mercato": str(plan_data.get("sentiment_mercato", ""))[:250],
            "ordini_pianificati": plan_data.get("ordini", []),
        }
        self._atomic_write_json(self.compatto_path, state)

        history = self.load_full_history()
        history["sessioni_giornaliere"].append(
            {"tipo": "morning_plan", "timestamp": _now_iso(), "dati": plan_data}
        )
        self._atomic_write_json(self.storico_path, history)

    def record_executed_orders(
        self,
        date_str: str,
        executed_orders: list[dict[str, Any]],
        current_positions: dict[str, Any],
    ) -> None:
        """Aggiorna le operazioni eseguite nella finestra degli ultimi 5 giorni operativi."""
        state = self.load_compact_state()
        history = self.load_full_history()

        state["ultimo_aggiornamento"] = _now_iso()
        state["posizioni_attuali"] = current_positions

        if executed_orders:
            days_list: list[dict[str, Any]] = state.get(
                "ultimi_5_giorni_operazioni", []
            )
            if days_list and days_list[-1].get("data") == date_str:
                days_list[-1]["operazioni"].extend(executed_orders)
            else:
                days_list.append({"data": date_str, "operazioni": executed_orders})
            state["ultimi_5_giorni_operazioni"] = days_list[
                -self.MAX_GIORNI_OPERAZIONI_COMPATTE :
            ]

            for order in executed_orders:
                history["tutte_le_operazioni"].append(
                    {"data": date_str, "timestamp": _now_iso(), **order}
                )

        self._atomic_write_json(self.compatto_path, state)
        self._atomic_write_json(self.storico_path, history)

    def update_end_of_day(
        self,
        date_str: str,
        nota_domani: str,
        nuova_regola: str | None,
        current_positions: dict[str, Any],
        pnl_report: dict[str, Any],
    ) -> None:
        """Aggiorna alle 21:05 il bilancio P&L vs SPY e prepara la memoria compatta per domani."""
        state = self.load_compact_state()
        history = self.load_full_history()

        if state.get("equity_iniziale") is None and pnl_report.get("equity_attuale"):
            state["equity_iniziale"] = pnl_report["equity_attuale"]
        if (
            state.get("spy_prezzo_riferimento_iniziale") is None
            and pnl_report.get("spy_prezzo_attuale")
        ):
            state["spy_prezzo_riferimento_iniziale"] = pnl_report["spy_prezzo_attuale"]

        state["ultimo_aggiornamento"] = _now_iso()
        state["nota_giorno_precedente"] = nota_domani.strip()[:600]
        state["posizioni_attuali"] = current_positions

        if nuova_regola and nuova_regola.strip():
            regola_clean = nuova_regola.strip()[:200]
            regole = state.get("regole_apprese", [])
            if regola_clean not in regole:
                regole.append(regola_clean)
            state["regole_apprese"] = regole[-self.MAX_REGOLE_APPRESE :]

        state["ultimi_5_giorni_operazioni"] = state.get(
            "ultimi_5_giorni_operazioni", []
        )[-self.MAX_GIORNI_OPERAZIONI_COMPATTE :]

        history["storico_bilanci"].append(
            {
                "data": date_str,
                "timestamp": _now_iso(),
                "pnl_report": pnl_report,
                "nota_giorno_precedente": state["nota_giorno_precedente"],
            }
        )

        self._atomic_write_json(self.compatto_path, state)
        self._atomic_write_json(self.storico_path, history)

    def get_prompt_payload(self) -> str:
        """Serializza in JSON ultra-compatto (< 2.500 token) lo stato per il prompt Gemini."""
        state = self.load_compact_state()
        return json.dumps(state, ensure_ascii=False, separators=(",", ":"))
