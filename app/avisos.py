"""Avisos al dueño: webhook propio (POST JSON) y/o Telegram. Si no hay nada configurado solo se loguea."""
from __future__ import annotations

import logging

import httpx

from .config import Config

log = logging.getLogger("ml.avisos")


def avisar(config: Config, http: httpx.Client, texto: str, datos: dict | None = None) -> list[str]:
    """Envía el aviso por los canales configurados. Devuelve los canales que funcionaron.

    Un canal que falla no corta el resto ni rompe el procesamiento de la orden.
    """
    enviados: list[str] = []
    if config.aviso_webhook_url:
        try:
            r = http.post(config.aviso_webhook_url, json={"texto": texto, "datos": datos or {}}, timeout=15)
            r.raise_for_status()
            enviados.append("webhook")
        except httpx.HTTPError as e:
            log.error("Falló el aviso por webhook: %s", e)
    if config.telegram_bot_token and config.telegram_chat_id:
        url = f"{config.telegram_api_url}/bot{config.telegram_bot_token}/sendMessage"
        try:
            r = http.post(url, json={"chat_id": config.telegram_chat_id, "text": texto}, timeout=15)
            r.raise_for_status()
            enviados.append("telegram")
        except httpx.HTTPError as e:
            # No se loguea la URL completa porque contiene el token del bot.
            log.error("Falló el aviso por Telegram: %s", type(e).__name__)
    if not enviados:
        log.info("AVISO (sin canal configurado o con error): %s", texto)
    return enviados
