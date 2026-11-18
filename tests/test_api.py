"""Pruebas del servidor FastAPI (webhook, OAuth y administración)."""
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from app import notificaciones  # noqa: E402
from app.main import crear_app  # noqa: E402


@pytest.fixture
def web(conectado):
    return TestClient(crear_app(conectado))


def test_salud(web):
    r = web.get("/salud")
    assert r.status_code == 200 and r.json()["autorizado"] is True


def test_flujo_oauth_por_http(servicios, sim, config):
    web = TestClient(crear_app(servicios))
    r = web.get("/auth/inicio", follow_redirects=False)
    assert r.status_code in (302, 307)
    destino = r.headers["location"]
    assert destino.startswith("https://auth.mercadolibre.com.ar/authorization?")
    # El simulador "aprueba" y redirige al callback con code y state
    import httpx
    redir = sim.manejar(httpx.Request("GET", destino))
    callback = redir.headers["Location"].replace("http://localhost:8000", "")
    r = web.get(callback)
    assert r.status_code == 200 and "123456789" in r.text
    assert servicios.tokens.user_id() == 123456789


def test_callback_sin_code(web):
    assert web.get("/auth/callback?error=access_denied").status_code == 400
    assert web.get("/auth/callback?code=TG-x&state=malo").status_code == 400


def test_webhook_responde_200_y_procesa(web, sim, conectado, config):
    n = sim.nueva_pregunta("MLA1000000001", "¿Hacen envíos?")
    r = web.post("/notificaciones", json=n)
    assert r.status_code == 200 and r.json()["resultado"] == "encolada"
    # TestClient corre las tareas en segundo plano antes de devolver
    assert notificaciones.estado_cola(config) == {"ok": 1}
    assert conectado.preguntas.listar("sugerida")[0]["regla"] == "envio"
    assert web.post("/notificaciones", json=n).json()["resultado"] == "duplicada"


def test_webhook_cuerpo_invalido_igual_200(web):
    r = web.post("/notificaciones", content=b"no es json", headers={"content-type": "application/json"})
    assert r.status_code == 200 and r.json()["resultado"] == "ignorada"
    assert web.post("/notificaciones", json=[1, 2]).json()["resultado"] == "ignorada"


def test_admin_exige_token(conectado, config):
    config.admin_token = "secreto-admin"
    web = TestClient(crear_app(conectado))
    assert web.get("/admin/ordenes").status_code == 401
    assert web.get("/admin/ordenes", headers={"X-Admin-Token": "otro"}).status_code == 401
    assert web.get("/admin/ordenes", headers={"X-Admin-Token": "secreto-admin"}).status_code == 200


def test_sync_por_api(web, sim):
    r = web.post("/admin/sync")
    assert r.status_code == 200 and r.json()["simulacro"] is True
    assert sim.puts == []
    r = web.post("/admin/sync?simulacro=false")
    assert len(r.json()["items_actualizados"]) == 3


def test_sync_fuente_rota(web, config, tmp_path):
    config.fuente_ruta = str(tmp_path / "no-existe.csv")
    assert web.post("/admin/sync").status_code == 400


def test_aprobar_pregunta_por_api(web, sim):
    web.post("/notificaciones", json=sim.nueva_pregunta("MLA1000000001", "¿Hacen factura?"))
    pendiente = web.get("/admin/preguntas?estado=sugerida").json()[0]
    r = web.post(f"/admin/preguntas/{pendiente['id']}/aprobar", json={})
    assert r.status_code == 200
    assert sim.preguntas[pendiente["id"]]["status"] == "ANSWERED"
    assert web.post(f"/admin/preguntas/{pendiente['id']}/aprobar").status_code == 409
    assert web.post("/admin/preguntas/1/aprobar").status_code == 404


def test_exportar_por_api(web, sim):
    web.post("/notificaciones", json=sim.nueva_orden("MLA1000000001"))
    r = web.get("/admin/ordenes/exportar?formato=csv")
    assert r.status_code == 200 and "Auriculares" in r.content.decode("utf-8-sig")
    assert web.get("/admin/ordenes/exportar?formato=pdf").status_code == 400


def test_sin_autorizar_devuelve_401(servicios):
    web = TestClient(crear_app(servicios))
    r = web.post("/admin/sync")
    assert r.status_code == 401 and r.json()["error"] == "autorizacion"
