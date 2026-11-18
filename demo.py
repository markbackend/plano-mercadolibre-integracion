"""Demo de 1 minuto: todo el flujo contra el simulador, sin internet, sin cuentas y sin claves.

    .venv/Scripts/python.exe demo.py
"""
from __future__ import annotations

import logging
import warnings
from pathlib import Path

import httpx

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*httpx2.*")  # aviso interno de starlette, no afecta
from fastapi.testclient import TestClient

from app import notificaciones, ordenes
from app.config import Config, RAIZ
from app.fuente import leer_fuente
from app.main import crear_app
from app.servicios import configurar_logs, crear_servicios
from app.simulador import CLIENT_ID_DEMO, CLIENT_SECRET_DEMO, SimuladorML, TransporteDemo
from app.sincronizacion import sincronizar


class Reloj:
    """Reloj manejable para mostrar la renovación del token sin esperar 6 horas."""
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def titulo(texto: str) -> None:
    print(f"\n=== {texto} ===")


def main() -> None:
    configurar_logs("WARNING")
    carpeta = RAIZ / "salidas" / "demo"
    carpeta.mkdir(parents=True, exist_ok=True)
    # Se arranca de cero: se borran la base y los exports de la corrida anterior.
    # (No se borra la carpeta entera porque en OneDrive/Windows a veces queda bloqueada.)
    for viejo in list(carpeta.glob("demo.db*")) + list(carpeta.glob("ordenes_*")):
        try:
            viejo.unlink()
        except OSError as e:
            raise SystemExit(f"No se pudo borrar {viejo} ({e}). Cerralo si está abierto y volvé a probar.")

    reloj = Reloj()
    sim = SimuladorML(reloj=reloj)
    transporte = TransporteDemo(sim)
    config = Config(client_id=CLIENT_ID_DEMO, client_secret=CLIENT_SECRET_DEMO,
                    db_path=str(carpeta / "demo.db"), salidas_dir=str(carpeta),
                    fuente_ruta=str(RAIZ / "ejemplos" / "stock_cliente.csv"),
                    preguntas_modo="sugerir", ia_activada=True, anthropic_api_key="clave-falsa-demo",
                    aviso_webhook_url="https://avisos.local/venta", espera_base_seg=0.01)
    s = crear_servicios(config, httpx.Client(transport=transporte), dormir=lambda _: None, reloj=reloj)

    titulo("1. OAuth")
    url, state = s.tokens.url_autorizacion()
    print("Link de autorización:", url)
    code = sim.emitir_code(config.redirect_uri)  # en la vida real lo trae Mercado Libre al callback
    datos = s.tokens.intercambiar_code(code, state)
    print(f"Cuenta conectada: user_id={datos['user_id']}, el token vence en {datos['expires_in']} s")

    titulo("2. Sincronización de stock y precio (SIMULACRO)")
    filas = leer_fuente(config)
    print(sincronizar(config, s.ml, filas, simulacro=True).resumen())
    print(f"PUT hechos al simulador: {len(sim.puts)} (el simulacro no toca nada)")

    titulo("3. Renovación automática del token + límite de la API (429)")
    reloj.t += 21600 - 60  # faltan 60 segundos para que venza
    sim.responder_429 = 2
    rep = sincronizar(config, s.ml, filas, simulacro=False)
    print(f"Refresh tokens usados: {sim.refresh_usados} (el token se renovó solo antes de vencer)")
    print(f"Hubo 2 respuestas 429; se reintentó con espera y siguió.")
    print(rep.resumen())
    v = sim.items["MLA1000000002"]["variations"]
    print("Remera en el simulador:", [(x["id"], x["available_quantity"], x["price"]) for x in v])

    titulo("4. Webhook: preguntas y una venta")
    web = TestClient(crear_app(s))
    eventos = [
        sim.nueva_pregunta("MLA1000000001", "Hola, ¿tenés stock?"),
        sim.nueva_pregunta("MLA1000000001", "¿Hacen factura A?"),
        sim.nueva_pregunta("MLA1000000001", "¿Cuánto dura la batería?"),
        sim.nueva_pregunta("MLA1000000003", "¿Sirve para Samsung S21?"),
        sim.nueva_pregunta("MLA1000000001", "Me llegó roto, quiero hacer un reclamo"),
        sim.nueva_orden("MLA1000000001", cantidad=2),
    ]
    for ev in eventos:
        r = web.post("/notificaciones", json=ev)
        print(f"  {ev['topic']:10s} {ev['resource']:22s} -> {r.status_code} {r.json()['resultado']}")
    r = web.post("/notificaciones", json=eventos[-1])  # Mercado Libre reintenta la misma notificación
    print(f"  reenvío de la misma venta -> {r.json()['resultado']}")
    notificaciones.procesar_pendientes(s)  # por si quedó algo (el webhook ya procesó en segundo plano)

    for p in s.preguntas.listar():
        print(f"  [{p['estado']:13s}] ({p['origen'] or '-':5s}) {p['texto']}")
        if p["respuesta"]:
            print(f"      -> {p['respuesta']}")
    primera = s.preguntas.listar("sugerida")[0]
    s.preguntas.aprobar(primera["id"])
    print(f"  Una persona aprobó la respuesta a la pregunta {primera['id']} y se envió a Mercado Libre.")

    titulo("5. Órdenes")
    for o in ordenes.listar(config):
        print(f"  Orden {o['id']} {o['estado']} {o['moneda']} {o['total']} - {o['comprador']}")
    print("  Avisos enviados al dueño:", len(transporte.avisos), "->", transporte.avisos[0]["cuerpo"]["texto"])
    print("  Excel:", ordenes.exportar(config, "xlsx").relative_to(RAIZ))
    print("  CSV:  ", ordenes.exportar(config, "csv").relative_to(RAIZ))
    print("\nDemo terminada. Base de la demo:", Path(config.db_path).relative_to(RAIZ))


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")  # tildes bien en la consola de Windows
    logging.getLogger().setLevel(logging.WARNING)
    main()
