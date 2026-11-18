"""Fixtures: cada prueba usa una base SQLite temporal y el simulador como transporte httpx falso.
Nada sale a internet y no hacen falta claves reales."""
from __future__ import annotations

import httpx
import pytest

from app.config import Config, RAIZ
from app.servicios import crear_servicios
from app.simulador import CLIENT_ID_DEMO, CLIENT_SECRET_DEMO, SimuladorML, TransporteDemo


class Reloj:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def reloj():
    return Reloj()


@pytest.fixture
def sim(reloj):
    return SimuladorML(reloj=reloj)


@pytest.fixture
def transporte(sim):
    return TransporteDemo(sim)


@pytest.fixture
def config(tmp_path):
    return Config(client_id=CLIENT_ID_DEMO, client_secret=CLIENT_SECRET_DEMO,
                  db_path=str(tmp_path / "prueba.db"), salidas_dir=str(tmp_path / "salidas"),
                  reglas_path=str(RAIZ / "config" / "reglas_preguntas.json"),
                  fuente_ruta=str(RAIZ / "ejemplos" / "stock_cliente.csv"),
                  espera_base_seg=1.0, max_reintentos=3)


@pytest.fixture
def esperas():
    return []


@pytest.fixture
def servicios(config, transporte, reloj, esperas):
    return crear_servicios(config, httpx.Client(transport=transporte), dormir=esperas.append, reloj=reloj)


@pytest.fixture
def conectado(servicios, sim, config):
    """Servicios con la cuenta del vendedor ya autorizada."""
    url, state = servicios.tokens.url_autorizacion()
    servicios.tokens.intercambiar_code(sim.emitir_code(config.redirect_uri), state)
    return servicios
