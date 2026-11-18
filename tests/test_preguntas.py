import json

import httpx
import pytest

from app.preguntas import cargar_reglas, normalizar, responder_por_reglas

ITEM = {"id": "MLA1", "title": "Auriculares", "price": 25999.0, "currency_id": "ARS",
        "available_quantity": 4, "condition": "new", "attributes": []}


@pytest.fixture
def reglas(config):
    return cargar_reglas(config.reglas_path)


def test_normalizar():
    assert normalizar("¿Tenés STOCK, envío?") == " tenes stock envio "


@pytest.mark.parametrize("pregunta,regla", [
    ("Hola tenés stock?", "stock"),
    ("¿Hacen envíos a Córdoba?", "envio"),
    ("Necesito factura A", "factura"),
    ("es nuevo?", "estado"),
    ("Cuánto sale?", "precio"),
])
def test_reglas(reglas, pregunta, regla):
    resp, nombre, derivar = responder_por_reglas(reglas, pregunta, ITEM)
    assert nombre == regla and not derivar
    assert resp.startswith("Hola!") and resp.endswith("Saludos.")


def test_plantilla_usa_datos_reales(reglas):
    resp, _, _ = responder_por_reglas(reglas, "hay stock?", ITEM)
    assert "4 unidades" in resp
    resp, _, _ = responder_por_reglas(reglas, "cuanto cuesta", ITEM)
    assert "$ 25.999,00" in resp


def test_sin_stock_usa_otra_plantilla(reglas):
    resp, nombre, _ = responder_por_reglas(reglas, "hay stock?", dict(ITEM, available_quantity=0))
    assert nombre == "sin_stock"


def test_no_inventa_si_falta_el_dato(reglas):
    resp, nombre, _ = responder_por_reglas(reglas, "es nuevo?", dict(ITEM, condition=None))
    assert resp is None


def test_deriva_a_persona(reglas):
    resp, nombre, derivar = responder_por_reglas(reglas, "Me llegó roto, quiero devolución y envío", ITEM)
    assert resp is None and derivar


def test_sin_regla(reglas):
    assert responder_por_reglas(reglas, "¿Es compatible con iPhone?", ITEM) == (None, None, False)


def test_modo_sugerir_y_aprobar(conectado, sim):
    n = sim.nueva_pregunta("MLA1000000001", "¿Tenés stock?")
    pid = int(n["resource"].split("/")[-1])
    r = conectado.preguntas.procesar(pid)
    assert r["estado"] == "sugerida" and r["origen"] == "regla"
    assert sim.preguntas[pid]["status"] == "UNANSWERED"  # no se respondió solo
    conectado.preguntas.aprobar(pid, "Hola! Sí, hay stock. Saludos.")
    assert sim.preguntas[pid]["answer"]["text"] == "Hola! Sí, hay stock. Saludos."
    fila = conectado.preguntas.listar("respondida")[0]
    assert fila["origen"] == "persona"
    with pytest.raises(ValueError):
        conectado.preguntas.aprobar(pid)


def test_modo_automatico(conectado, sim, config):
    config.preguntas_modo = "automatico"
    n = sim.nueva_pregunta("MLA1000000001", "¿Hacen factura?")
    pid = int(n["resource"].split("/")[-1])
    assert conectado.preguntas.procesar(pid)["estado"] == "respondida"
    assert "factura" in sim.preguntas[pid]["answer"]["text"]
    # Procesar de nuevo (notificación repetida) no hace nada
    assert conectado.preguntas.procesar(pid)["nuevo"] is False


def test_modo_apagado(conectado, sim, config):
    config.preguntas_modo = "apagado"
    n = sim.nueva_pregunta("MLA1000000001", "¿Hacen factura?")
    pid = int(n["resource"].split("/")[-1])
    assert conectado.preguntas.procesar(pid)["estado"] == "sin_respuesta"
    assert sim.preguntas[pid]["status"] == "UNANSWERED"


def test_pregunta_ya_respondida_afuera(conectado, sim):
    n = sim.nueva_pregunta("MLA1000000001", "hola")
    pid = int(n["resource"].split("/")[-1])
    sim.preguntas[pid].update(status="ANSWERED", answer={"text": "respondida desde la web"})
    assert conectado.preguntas.procesar(pid)["estado"] == "respondida"


def test_ia_usa_datos_de_la_publicacion(conectado, sim, config, transporte):
    config.ia_activada = True
    config.anthropic_api_key = "clave-de-prueba"
    enviados = []

    def ia(req):
        enviados.append(req)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Hola! La batería dura 20 h. Saludos."}]})

    transporte.ia = ia
    n = sim.nueva_pregunta("MLA1000000001", "¿Cuánto dura la batería?")
    pid = int(n["resource"].split("/")[-1])
    r = conectado.preguntas.procesar(pid)
    assert r["estado"] == "sugerida" and r["origen"] == "ia"
    req = enviados[0]
    assert req.url == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "clave-de-prueba"
    assert req.headers["anthropic-version"] == "2023-06-01"
    cuerpo = json.loads(req.content)
    assert cuerpo["model"] == "claude-haiku-5-5"
    assert "SIN_DATOS" in cuerpo["system"]
    assert "Duración de la batería" in cuerpo["messages"][0]["content"]


def test_ia_sin_datos_queda_para_persona(conectado, sim, config):
    config.ia_activada = True
    config.anthropic_api_key = "clave-de-prueba"
    n = sim.nueva_pregunta("MLA1000000003", "¿Es compatible con el Samsung S21?")
    pid = int(n["resource"].split("/")[-1])
    assert conectado.preguntas.procesar(pid)["estado"] == "sin_respuesta"


def test_ia_caida_no_rompe(conectado, sim, config, transporte):
    config.ia_activada = True
    config.anthropic_api_key = "clave-de-prueba"
    transporte.ia = lambda req: httpx.Response(529, json={"type": "error"})
    n = sim.nueva_pregunta("MLA1000000003", "¿Es compatible con el Samsung S21?")
    pid = int(n["resource"].split("/")[-1])
    assert conectado.preguntas.procesar(pid)["estado"] == "sin_respuesta"


def test_ia_no_se_usa_si_hay_que_derivar(conectado, sim, config, transporte):
    config.ia_activada = True
    config.anthropic_api_key = "clave-de-prueba"
    llamadas = []
    transporte.ia = lambda req: llamadas.append(req) or httpx.Response(500)
    n = sim.nueva_pregunta("MLA1000000001", "quiero hacer un reclamo")
    conectado.preguntas.procesar(int(n["resource"].split("/")[-1]))
    assert llamadas == []
