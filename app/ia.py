"""Respuestas sugeridas con IA (Anthropic Claude) por httpx, sin SDK.

La IA solo puede usar los datos de la publicación y las políticas que le pasamos. Si la respuesta
no está en esos datos tiene que contestar exactamente SIN_DATOS, y en ese caso la pregunta queda
para que la responda una persona. Nunca se inventan medidas, plazos ni compatibilidades.
"""
from __future__ import annotations

import json
import logging

import httpx

from .config import Config

log = logging.getLogger("ml.ia")

MARCADOR_SIN_DATOS = "SIN_DATOS"

SISTEMA = (
    "Respondés preguntas de compradores en Mercado Libre en nombre de un vendedor de Argentina. "
    "Reglas estrictas:\n"
    "1. Usá SOLO la información de DATOS_PUBLICACION y POLITICAS_VENDEDOR. No supongas nada.\n"
    "2. Si la respuesta no está en esos datos, respondé exactamente: SIN_DATOS\n"
    "3. Máximo 350 caracteres, en español, tono amable y simple, sin emojis.\n"
    "4. No incluyas teléfonos, emails, links ni pidas datos de contacto (Mercado Libre lo prohíbe).\n"
    "5. Devolvé solo el texto de la respuesta, sin comillas ni explicaciones."
)


class ErrorIA(Exception):
    pass


def datos_publicacion(item: dict) -> dict:
    atributos = {a.get("name") or a.get("id"): a.get("value_name")
                 for a in item.get("attributes") or [] if a.get("value_name")}
    return {
        "titulo": item.get("title"),
        "precio": item.get("price"),
        "moneda": item.get("currency_id"),
        "stock_disponible": item.get("available_quantity"),
        "condicion": item.get("condition"),
        "envio_gratis": (item.get("shipping") or {}).get("free_shipping"),
        "atributos": atributos,
    }


def sugerir_respuesta(config: Config, http: httpx.Client, pregunta: str, item: dict,
                      politicas: list[str]) -> str | None:
    """Devuelve el texto sugerido o None si la IA no tiene datos suficientes."""
    if not config.anthropic_api_key:
        raise ErrorIA("Falta ANTHROPIC_API_KEY")
    contenido = (
        "DATOS_PUBLICACION:\n" + json.dumps(datos_publicacion(item), ensure_ascii=False, indent=1)
        + "\n\nPOLITICAS_VENDEDOR:\n" + "\n".join(f"- {p}" for p in politicas)
        + f"\n\nPREGUNTA DEL COMPRADOR:\n{pregunta}"
    )
    try:
        r = http.post(
            config.anthropic_url,
            headers={"x-api-key": config.anthropic_api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": config.anthropic_modelo, "max_tokens": 300, "system": SISTEMA,
                  "messages": [{"role": "user", "content": contenido}]},
            timeout=30,
        )
    except httpx.HTTPError as e:
        raise ErrorIA(f"No se pudo contactar a la IA: {e}") from e
    if r.status_code != 200:
        raise ErrorIA(f"La IA respondió {r.status_code}: {r.text[:300]}")
    datos = r.json()
    texto = "".join(b.get("text", "") for b in datos.get("content", []) if b.get("type") == "text").strip()
    if not texto or MARCADOR_SIN_DATOS in texto:
        log.info("La IA no tiene datos suficientes para responder")
        return None
    return texto[:2000]
