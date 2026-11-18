"""Arma todos los objetos (cliente HTTP, tokens, API, preguntas) a partir de la configuración.

En producción `http` es un httpx.Client normal; en las pruebas y la demo se le pasa un
httpx.Client con un transporte falso (el simulador), así nada sale a internet.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from .cliente_ml import ClienteML
from .config import Config
from .db import inicializar
from .oauth import GestorTokens
from .preguntas import GestorPreguntas


@dataclass
class Servicios:
    config: Config
    http: httpx.Client
    tokens: GestorTokens
    ml: ClienteML
    preguntas: GestorPreguntas


def crear_servicios(config: Config, http: httpx.Client | None = None,
                    dormir: Callable[[float], None] = time.sleep,
                    reloj: Callable[[], float] = time.time) -> Servicios:
    inicializar(config.db_path)
    http = http or httpx.Client(timeout=30)
    tokens = GestorTokens(config, http, reloj=reloj, dormir=dormir)
    ml = ClienteML(config, tokens, http, dormir=dormir)
    return Servicios(config, http, tokens, ml, GestorPreguntas(config, ml, http))


def configurar_logs(nivel: str = "INFO") -> None:
    logging.basicConfig(level=getattr(logging, nivel.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # httpx loguea cada URL en INFO; se baja para no llenar el log (y no mostrar parámetros).
    logging.getLogger("httpx").setLevel(logging.WARNING)
