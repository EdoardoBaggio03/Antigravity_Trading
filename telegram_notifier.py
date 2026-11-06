"""Modulo di notifica Telegram resiliente per AI Capital."""

from __future__ import annotations

import logging
import requests
import config

logger = logging.getLogger("AICapital.Telegram")


class TelegramNotifier:
    """Invia report e alert operativi su Telegram gestendo timeout ed errori di rete senza crash."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        timeout: int = 8,
    ) -> None:
        self.bot_token = (
            bot_token if bot_token is not None else config.TELEGRAM_BOT_TOKEN
        )
        self.chat_id = chat_id if chat_id is not None else config.TELEGRAM_CHAT_ID
        self.timeout = timeout

    def send_message(self, text: str) -> bool:
        """Invia un messaggio a Telegram; restituisce False senza sollevare eccezioni in caso di timeout/errore."""
        if not self.bot_token or not self.chat_id:
            logger.info(
                "Telegram non configurato (token/chat_id mancanti). Messaggio locale:\n%s",
                text,
            )
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Impossibile inviare notifica Telegram (gestito senza crash): %s", exc)
            return False
