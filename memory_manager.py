"""Memoria compatta anti-sprechi di AI Capital (storico completo su file, solo l'essenziale nel prompt)."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
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
        "nota_giorno_precedente": "Giorno 0: portafoglio appena inizializzato.",
        "regole_apprese": [
            "Tagliare rapidamente le perdite e lasciar correre i trade vincenti.",
            "Non aumentare l'esposizione su strategie con aspettativa negativa.",
        ],
        "parametri_strategia": dict(config.DEFAULT_STRATEGY_PARAMS),
        "watchlist_dinamica": [],
        "sessioni_eseguite": {"data": None, "morning": False, "close": False},
        "piano_mattutino_corrente": {},
        "posizioni_attuali": {},
        "ultimi_5_giorni_operazioni": [],
    }


def _default_storico_completo() -> dict[str, Any]:
    return {"creato_il": _now_iso(), "sessioni_giornaliere": [], "tutte_le_operazioni": [], "storico_bilanci": []}


class MemoryManager:
    """Gestisce `storico_completo.json` e `stato_compatto.json` (payload per il prompt sempre compatto)."""

    MAX_REGOLE_APPRESE = 10
    MAX_GIORNI_OPERAZIONI_COMPATTE = 5
    MAX_OPERAZIONI_PER_GIORNO = 40
    MAX_WATCHLIST = 15

    def __init__(self, storico_path: Path | None = None, compatto_path: Path | None = None) -> None:
        self.storico_path = storico_path or config.STORICO_COMPLETO_PATH
        self.compatto_path = compatto_path or config.STATO_COMPATTO_PATH
        self.storico_path.parent.mkdir(parents=True, exist_ok=True)
        self.compatto_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ I/O resiliente
    def _safe_load_json(self, path: Path, default_factory: Callable[[], dict[str, Any]]) -> dict[str, Any]:
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
            logger.warning("File memoria %s corrotto (%s). Backup salvato in %s e ripristino stato pulito.",
                           path.name, exc, backup_path.name)
            data = default_factory()
            self._atomic_write_json(path, data)
            return data

    def _atomic_write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    # ------------------------------------------------------------------ lettura
    def load_compact_state(self) -> dict[str, Any]:
        state = self._safe_load_json(self.compatto_path, _default_stato_compatto)
        for k, v in _default_stato_compatto().items():
            state.setdefault(k, v)
        state["regole_apprese"] = list(state.get("regole_apprese", []))[-self.MAX_REGOLE_APPRESE:]
        state["ultimi_5_giorni_operazioni"] = list(state.get("ultimi_5_giorni_operazioni", []))[-self.MAX_GIORNI_OPERAZIONI_COMPATTE:]
        state["nota_giorno_precedente"] = str(state.get("nota_giorno_precedente", ""))[:600]
        state["parametri_strategia"] = config.sanitize_strategy_params(state.get("parametri_strategia"))
        return state

    def load_full_history(self) -> dict[str, Any]:
        history = self._safe_load_json(self.storico_path, _default_storico_completo)
        for k, v in _default_storico_completo().items():
            history.setdefault(k, v)
        return history

    def get_strategy_params(self) -> dict[str, Any]:
        return self.load_compact_state()["parametri_strategia"]

    def get_prompt_payload(self) -> str:
        """JSON ultra-compatto per il prompt: niente duplicati (posizioni e piano arrivano già dal conto)."""
        state = self.load_compact_state()
        recent_ops: list[list[Any]] = []
        per_day: dict[str, int] = {}
        for day in state.get("ultimi_5_giorni_operazioni", []):
            ops = day.get("operazioni", [])
            per_day[str(day.get("data"))] = len(ops)
            for op in ops:
                recent_ops.append([day.get("data"), op.get("azione"), op.get("ticker"),
                                   op.get("quantita"), op.get("strategia", "")])
        compact = {
            "nota": state.get("nota_giorno_precedente", ""),
            "regole_apprese": state.get("regole_apprese", []),
            "watchlist_dinamica": state.get("watchlist_dinamica", []),
            "operazioni_per_giorno": per_day,
            "ultime_operazioni": recent_ops[-10:],
        }
        return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    # ------------------------------------------------------------------ scrittura
    def save_strategy_params(self, params: dict[str, Any], watchlist: list[str] | None = None) -> dict[str, Any]:
        state = self.load_compact_state()
        state["parametri_strategia"] = config.sanitize_strategy_params(params)
        if watchlist is not None:
            clean = [str(t).strip().upper() for t in watchlist if str(t).strip()]
            state["watchlist_dinamica"] = list(dict.fromkeys(clean))[: self.MAX_WATCHLIST]
        state["ultimo_aggiornamento"] = _now_iso()
        self._atomic_write_json(self.compatto_path, state)
        return state["parametri_strategia"]

    def is_session_done(self, date_str: str, name: str) -> bool:
        flags = self.load_compact_state().get("sessioni_eseguite", {})
        return flags.get("data") == date_str and bool(flags.get(name))

    def mark_session_done(self, date_str: str, name: str) -> None:
        state = self.load_compact_state()
        flags = state.get("sessioni_eseguite", {})
        if flags.get("data") != date_str:
            flags = {"data": date_str, "morning": False, "close": False}
        flags[name] = True
        state["sessioni_eseguite"] = flags
        self._atomic_write_json(self.compatto_path, state)

    def save_morning_plan(self, plan_data: dict[str, Any]) -> None:
        state = self.load_compact_state()
        state["ultimo_aggiornamento"] = _now_iso()
        state["piano_mattutino_corrente"] = {
            "timestamp": _now_iso(),
            "sentiment_mercato": str(plan_data.get("sentiment_mercato", ""))[:250],
            "ordini_pianificati": plan_data.get("ordini", []),
            "parametri": plan_data.get("parametri", {}),
        }
        self._atomic_write_json(self.compatto_path, state)

        history = self.load_full_history()
        history["sessioni_giornaliere"].append({"tipo": "morning_plan", "timestamp": _now_iso(), "dati": plan_data})
        history["sessioni_giornaliere"] = history["sessioni_giornaliere"][-300:]
        self._atomic_write_json(self.storico_path, history)

    def record_executed_orders(self, date_str: str, executed_orders: list[dict[str, Any]],
                               current_positions: dict[str, Any]) -> None:
        state = self.load_compact_state()
        history = self.load_full_history()
        state["ultimo_aggiornamento"] = _now_iso()
        state["posizioni_attuali"] = current_positions

        if executed_orders:
            days_list: list[dict[str, Any]] = state.get("ultimi_5_giorni_operazioni", [])
            if days_list and days_list[-1].get("data") == date_str:
                days_list[-1]["operazioni"].extend(executed_orders)
                days_list[-1]["operazioni"] = days_list[-1]["operazioni"][-self.MAX_OPERAZIONI_PER_GIORNO:]
            else:
                days_list.append({"data": date_str, "operazioni": executed_orders})
            state["ultimi_5_giorni_operazioni"] = days_list[-self.MAX_GIORNI_OPERAZIONI_COMPATTE:]
            for order in executed_orders:
                history["tutte_le_operazioni"].append({"data": date_str, "timestamp": _now_iso(), **order})

        self._atomic_write_json(self.compatto_path, state)
        self._atomic_write_json(self.storico_path, history)

    def update_end_of_day(self, date_str: str, nota_domani: str, nuova_regola: str | None,
                          current_positions: dict[str, Any], pnl_report: dict[str, Any]) -> None:
        state = self.load_compact_state()
        history = self.load_full_history()

        if state.get("equity_iniziale") is None and pnl_report.get("equity_attuale"):
            state["equity_iniziale"] = pnl_report["equity_attuale"]
        if state.get("spy_prezzo_riferimento_iniziale") is None and pnl_report.get("spy_prezzo_attuale"):
            state["spy_prezzo_riferimento_iniziale"] = pnl_report["spy_prezzo_attuale"]

        state["ultimo_aggiornamento"] = _now_iso()
        state["nota_giorno_precedente"] = nota_domani.strip()[:600]
        state["posizioni_attuali"] = current_positions

        if nuova_regola and nuova_regola.strip():
            regola = nuova_regola.strip()[:200]
            regole = state.get("regole_apprese", [])
            if regola not in regole:
                regole.append(regola)
            state["regole_apprese"] = regole[-self.MAX_REGOLE_APPRESE:]

        history["storico_bilanci"].append({
            "data": date_str, "timestamp": _now_iso(), "pnl_report": pnl_report,
            "nota_giorno_precedente": state["nota_giorno_precedente"],
        })
        self._atomic_write_json(self.compatto_path, state)
        self._atomic_write_json(self.storico_path, history)
