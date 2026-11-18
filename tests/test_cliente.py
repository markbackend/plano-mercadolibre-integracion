import httpx
import pytest

from app.cliente_ml import ClienteML, ErrorML


def test_429_respeta_retry_after(conectado, sim, esperas):
    sim.responder_429 = 2
    assert conectado.ml.yo()["nickname"] == "VENDEDOR_DEMO"
    assert esperas == [1.0, 1.0]


def test_429_sin_fin_termina_en_error(conectado, sim, esperas, config):
    sim.responder_429 = 100
    with pytest.raises(ErrorML) as e:
        conectado.ml.yo()
    assert e.value.status == 429
    assert len(esperas) == config.max_reintentos


def test_5xx_espera_exponencial(conectado, config):
    llamadas = {"n": 0}

    def manejar(req):
        llamadas["n"] += 1
        if llamadas["n"] < 3:
            return httpx.Response(503, json={"error": "service_unavailable"})
        return httpx.Response(200, json={"ok": True})

    esperas = []
    ml = ClienteML(config, conectado.tokens, httpx.Client(transport=httpx.MockTransport(manejar)),
                   dormir=esperas.append)
    assert ml.pedir("GET", "/algo") == {"ok": True}
    assert esperas == [1.0, 2.0]


def test_error_de_red_reintenta(conectado, config):
    llamadas = {"n": 0}

    def manejar(req):
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            raise httpx.ConnectError("sin conexión")
        return httpx.Response(200, json={"ok": 1})

    ml = ClienteML(config, conectado.tokens, httpx.Client(transport=httpx.MockTransport(manejar)),
                   dormir=lambda s: None)
    assert ml.pedir("GET", "/x") == {"ok": 1}


def test_404_no_reintenta(conectado, esperas):
    with pytest.raises(ErrorML) as e:
        conectado.ml.item("MLA999")
    assert e.value.status == 404
    assert esperas == []


def test_busqueda_pagina(conectado, sim):
    for i in range(60):
        nuevo = dict(sim.items["MLA1000000003"], id=f"MLA77{i:08d}")
        sim.items[nuevo["id"]] = nuevo
    ids = conectado.ml.buscar_items_vendedor(123456789)
    assert len(ids) == 65
    assert len(set(ids)) == 65


def test_multiget_en_lotes_de_20(conectado, sim):
    ids = list(sim.items)
    for i in range(30):
        nuevo = dict(sim.items["MLA1000000003"], id=f"MLA88{i:08d}")
        sim.items[nuevo["id"]] = nuevo
        ids.append(nuevo["id"])
    items = conectado.ml.items(ids + ["MLA_NO_EXISTE"])
    assert len(items) == len(ids)
    assert sum(1 for m, r in sim.registro if r == "/items") == 2
