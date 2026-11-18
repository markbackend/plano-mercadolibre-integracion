"""Simulador de Mercado Libre como servidor HTTP local (para probar el servidor real a mano).

    .venv/Scripts/python.exe -m app.simulador_servidor        (escucha en 127.0.0.1:8001)

Configurar la app con ML_API_URL=http://127.0.0.1:8001 y ML_AUTH_URL=http://127.0.0.1:8001,
ML_CLIENT_ID=1234567890123456 y ML_CLIENT_SECRET=secreto-demo (ver README, "Demo con servidores").

Endpoints extra del simulador (para disparar eventos):
    POST /_sim/pregunta  {"item_id": "MLA1000000001", "texto": "¿Hay stock?"}
    POST /_sim/orden     {"item_id": "MLA1000000001", "cantidad": 1}
Cada uno crea el recurso y manda la notificación al webhook de la app (APP_WEBHOOK_URL).
"""
from __future__ import annotations

import logging
import os

import httpx
from fastapi import FastAPI, Request, Response

from .simulador import SimuladorML

log = logging.getLogger("ml.simulador")


def crear_app_simulador(sim: SimuladorML | None = None, webhook: str | None = None) -> FastAPI:
    sim = sim or SimuladorML()
    webhook = webhook or os.environ.get("APP_WEBHOOK_URL", "http://127.0.0.1:8000/notificaciones")
    app = FastAPI(title="Simulador de Mercado Libre")
    app.state.sim = sim

    def _avisar(notif: dict) -> dict:
        try:
            r = httpx.post(webhook, json=notif, timeout=10)
            log.info("Notificación %s enviada a %s -> %s", notif["topic"], webhook, r.status_code)
            return {"notificacion": notif, "webhook_status": r.status_code}
        except httpx.HTTPError as e:
            log.warning("No se pudo avisar al webhook %s: %s", webhook, e)
            return {"notificacion": notif, "webhook_status": None, "error": str(e)}

    @app.post("/_sim/pregunta")
    async def sim_pregunta(request: Request):
        d = await request.json()
        return _avisar(sim.nueva_pregunta(d["item_id"], d["texto"]))

    @app.post("/_sim/orden")
    async def sim_orden(request: Request):
        d = await request.json()
        return _avisar(sim.nueva_orden(d["item_id"], int(d.get("cantidad", 1)), d.get("variacion_id")))

    @app.api_route("/{ruta:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def todo(ruta: str, request: Request):
        cuerpo = await request.body()
        req = httpx.Request(request.method, str(request.url), headers=list(request.headers.items()),
                            content=cuerpo)
        r = sim.manejar(req)
        return Response(content=r.content, status_code=r.status_code,
                        headers={k: v for k, v in r.headers.items()
                                 if k.lower() in ("content-type", "location", "retry-after")})

    return app


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    uvicorn.run(crear_app_simulador(), host="127.0.0.1", port=int(os.environ.get("SIM_PUERTO", "8001")))
