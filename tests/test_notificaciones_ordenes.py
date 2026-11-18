import csv

import httpx
from openpyxl import load_workbook

from app import notificaciones, ordenes
from app.db import conectar


def test_registrar_y_duplicados(conectado, sim, config):
    n = sim.nueva_orden("MLA1000000001")
    assert notificaciones.registrar(config, n)[0] == "encolada"
    assert notificaciones.registrar(config, n)[0] == "duplicada"  # mismo _id
    otra = dict(n, _id="otro-id")  # mismo recurso, todavía pendiente: se agrupa
    assert notificaciones.registrar(config, otra)[0] == "duplicada"
    assert notificaciones.estado_cola(config) == {"pendiente": 1}


def test_ignora_topicos_y_apps_ajenas(config):
    assert notificaciones.registrar(config, {"topic": "payments", "resource": "/x/1"})[0] == "ignorada"
    assert notificaciones.registrar(config, {"topic": "orders_v2"})[0] == "ignorada"
    ajena = {"topic": "orders_v2", "resource": "/orders/1", "application_id": 999}
    assert notificaciones.registrar(config, ajena)[0] == "ignorada"


def test_orden_se_guarda_y_avisa_una_vez(conectado, sim, config, transporte):
    config.aviso_webhook_url = "https://avisos.local/venta"
    config.telegram_bot_token = "123:abc"
    config.telegram_chat_id = "42"
    n = sim.nueva_orden("MLA1000000002", cantidad=2, variacion_id=9001)
    notificaciones.registrar(config, n)
    assert notificaciones.procesar_pendientes(conectado) == {"ok": 1, "error": 0}
    lista = ordenes.listar(config)
    assert len(lista) == 1
    assert lista[0]["items"][0]["sku"] == "REM-NEG-M"
    assert {a["host"] for a in transporte.avisos} == {"avisos.local", "api.telegram.org"}
    assert transporte.avisos[1]["cuerpo"]["chat_id"] == "42"
    # La misma orden vuelve a notificarse (cambió de estado): se actualiza pero no se re-avisa.
    notificaciones.registrar(config, dict(n, _id="segunda"))
    notificaciones.procesar_pendientes(conectado)
    assert len(transporte.avisos) == 2
    assert len(ordenes.listar(config)) == 1


def test_orden_no_pagada_no_avisa_hasta_pagarse(conectado, sim, config, transporte):
    config.aviso_webhook_url = "https://avisos.local/venta"
    n = sim.nueva_orden("MLA1000000001", estado="payment_required")
    oid = int(n["resource"].split("/")[-1])
    notificaciones.registrar(config, n)
    notificaciones.procesar_pendientes(conectado)
    assert transporte.avisos == []
    sim.ordenes[oid]["status"] = "paid"
    notificaciones.registrar(config, dict(n, _id="pagada"))
    notificaciones.procesar_pendientes(conectado)
    assert len(transporte.avisos) == 1
    assert ordenes.listar(config)[0]["estado"] == "paid"


def test_un_canal_caido_no_rompe_si_otro_funciona(conectado, sim, config, transporte):
    config.aviso_webhook_url = "https://webhook-caido.local/x"
    config.telegram_bot_token, config.telegram_chat_id = "123:abc", "42"
    transporte.extra["webhook-caido.local"] = lambda r: httpx.Response(500)
    notificaciones.registrar(config, sim.nueva_orden("MLA1000000001"))
    assert notificaciones.procesar_pendientes(conectado) == {"ok": 1, "error": 0}
    assert [a["host"] for a in transporte.avisos] == ["api.telegram.org"]


def test_si_fallan_todos_los_avisos_se_reintenta(conectado, sim, config, transporte):
    config.aviso_webhook_url = "https://webhook-caido.local/x"
    caido = {"si": True}
    transporte.extra["webhook-caido.local"] = (
        lambda r: httpx.Response(500) if caido["si"] else httpx.Response(200, json={"ok": True}))
    notificaciones.registrar(config, sim.nueva_orden("MLA1000000001"))
    assert notificaciones.procesar_pendientes(conectado) == {"ok": 0, "error": 1}
    assert ordenes.listar(config)[0]["avisada"] == 0  # la orden quedó guardada, el aviso pendiente
    caido["si"] = False
    assert notificaciones.procesar_pendientes(conectado) == {"ok": 1, "error": 0}
    assert ordenes.listar(config)[0]["avisada"] == 1


def test_notificacion_trabada_vuelve_a_la_cola(conectado, sim, config):
    notificaciones.registrar(config, sim.nueva_orden("MLA1000000001"))
    con = conectar(config.db_path)
    # Simula un worker que se cayó hace 30 minutos a mitad del procesamiento.
    con.execute("UPDATE notificaciones SET estado='procesando', intentos=1, tomado=datetime('now','-30 minutes')")
    con.close()
    assert notificaciones.procesar_pendientes(conectado) == {"ok": 1, "error": 0}


def test_notificacion_en_proceso_reciente_no_se_toca(conectado, sim, config):
    notificaciones.registrar(config, sim.nueva_orden("MLA1000000001"))
    con = conectar(config.db_path)
    con.execute("UPDATE notificaciones SET estado='procesando', intentos=1, tomado=datetime('now')")
    con.close()
    assert notificaciones.procesar_pendientes(conectado) == {"ok": 0, "error": 0}


def test_notificacion_de_item_actualiza_cache(conectado, sim, config):
    notificaciones.registrar(config, {"topic": "items", "resource": "/items/MLA1000000003", "_id": "i1"})
    notificaciones.procesar_pendientes(conectado)
    con = conectar(config.db_path)
    fila = con.execute("SELECT * FROM items_cache").fetchone()
    con.close()
    assert fila["id"] == "MLA1000000003" and fila["stock"] == 2


def test_error_se_reintenta(conectado, sim, config):
    notificaciones.registrar(config, {"topic": "orders_v2", "resource": "/orders/1", "_id": "x"})
    assert notificaciones.procesar_pendientes(conectado) == {"ok": 0, "error": 1}
    sim.ordenes[1] = {"id": 1, "status": "paid", "total_amount": 10, "currency_id": "ARS",
                      "buyer": {"nickname": "B"}, "order_items": []}
    assert notificaciones.procesar_pendientes(conectado) == {"ok": 1, "error": 0}


def test_error_definitivo_tras_max_intentos(conectado, config):
    notificaciones.registrar(config, {"topic": "orders_v2", "resource": "/orders/1", "_id": "x"})
    for _ in range(notificaciones.MAX_INTENTOS + 2):
        notificaciones.procesar_pendientes(conectado)
    con = conectar(config.db_path)
    fila = con.execute("SELECT * FROM notificaciones").fetchone()
    con.close()
    assert fila["intentos"] == notificaciones.MAX_INTENTOS and fila["estado"] == "error"


def test_exportar_xlsx_y_csv(conectado, sim, config):
    for item in ("MLA1000000001", "MLA1000000003"):
        notificaciones.registrar(config, sim.nueva_orden(item))
    notificaciones.procesar_pendientes(conectado)
    xlsx = ordenes.exportar(config, "xlsx")
    hoja = load_workbook(xlsx).active
    filas = list(hoja.iter_rows(values_only=True))
    assert filas[0] == tuple(ordenes.COLUMNAS)
    assert len(filas) == 3
    ruta_csv = ordenes.exportar(config, "csv")
    with open(ruta_csv, encoding="utf-8-sig") as f:
        lineas = list(csv.reader(f, delimiter=";"))
    assert lineas[0] == ordenes.COLUMNAS and len(lineas) == 3
    assert "Cargador USB-C 20W" in "".join(lineas[1] + lineas[2])
