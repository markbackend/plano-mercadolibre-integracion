"""Servidor FastAPI: OAuth, webhook de notificaciones y endpoints de administración.

Arranque: uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import logging
import secrets

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from . import notificaciones, ordenes
from .cliente_ml import ErrorML
from .config import cargar_config
from .fuente import ErrorFuente, leer_fuente
from .oauth import ErrorAutorizacion
from .servicios import Servicios, configurar_logs, crear_servicios
from .sincronizacion import sincronizar

log = logging.getLogger("ml.web")


class Aprobacion(BaseModel):
    texto: str | None = None


def crear_app(servicios: Servicios | None = None) -> FastAPI:
    if servicios is None:
        configurar_logs()
        servicios = crear_servicios(cargar_config())
    s = servicios
    app = FastAPI(title="Integración Mercado Libre", version="1.0.0")
    app.state.servicios = s

    def admin(x_admin_token: str | None = Header(default=None)):
        """Si ADMIN_TOKEN está configurado, los endpoints de administración lo exigen."""
        if s.config.admin_token and not secrets.compare_digest(x_admin_token or "", s.config.admin_token):
            raise HTTPException(401, "Falta o es incorrecto el encabezado X-Admin-Token")

    @app.exception_handler(ErrorAutorizacion)
    async def _sin_autorizacion(request: Request, exc: ErrorAutorizacion):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=401, content={"error": "autorizacion", "detalle": str(exc)})

    @app.exception_handler(ErrorML)
    async def _error_ml(request: Request, exc: ErrorML):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=502, content={"error": "mercadolibre", "status": exc.status,
                                                      "detalle": exc.detalle})

    @app.get("/salud")
    def salud():
        return {"ok": True, "autorizado": s.tokens.user_id() is not None,
                "cola": notificaciones.estado_cola(s.config)}

    # ---------- OAuth ----------
    @app.get("/auth/inicio")
    def auth_inicio():
        url, _ = s.tokens.url_autorizacion()
        return RedirectResponse(url)

    @app.get("/auth/callback", response_class=HTMLResponse)
    def auth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
        if error or not code:
            raise HTTPException(400, f"Mercado Libre no devolvió el código de autorización ({error or 'sin code'})")
        try:
            datos = s.tokens.intercambiar_code(code, state)
        except ErrorAutorizacion as e:
            log.error("Fallo en el callback de OAuth: %s", e)
            raise HTTPException(400, str(e))
        return f"<p>Listo: la cuenta {datos.get('user_id')} quedó conectada. Ya podés cerrar esta pestaña.</p>"

    # ---------- Webhook ----------
    @app.post("/notificaciones")
    async def recibir_notificacion(request: Request, tareas: BackgroundTasks):
        """Responde 200 enseguida; el procesamiento va en segundo plano."""
        try:
            cuerpo = await request.json()
        except ValueError:
            log.warning("Notificación con cuerpo inválido")
            return {"resultado": "ignorada"}
        if not isinstance(cuerpo, dict):
            return {"resultado": "ignorada"}
        resultado, nid = notificaciones.registrar(s.config, cuerpo)
        if resultado == "encolada":
            tareas.add_task(notificaciones.procesar_pendientes, s)
        return {"resultado": resultado, "id": nid}

    # ---------- Administración ----------
    @app.post("/admin/notificaciones/procesar", dependencies=[Depends(admin)])
    def procesar_cola():
        return notificaciones.procesar_pendientes(s)

    @app.get("/admin/notificaciones", dependencies=[Depends(admin)])
    def estado_notificaciones():
        return notificaciones.estado_cola(s.config)

    @app.post("/admin/sync", dependencies=[Depends(admin)])
    def sync(simulacro: bool = True):
        try:
            filas = leer_fuente(s.config, s.http)
        except ErrorFuente as e:
            raise HTTPException(400, f"Error en la fuente del cliente: {e}")
        return sincronizar(s.config, s.ml, filas, simulacro=simulacro).a_dict()

    @app.get("/admin/preguntas", dependencies=[Depends(admin)])
    def listar_preguntas(estado: str | None = None):
        return s.preguntas.listar(estado)

    @app.post("/admin/preguntas/{pregunta_id}/aprobar", dependencies=[Depends(admin)])
    def aprobar(pregunta_id: int, cuerpo: Aprobacion | None = None):
        try:
            return s.preguntas.aprobar(pregunta_id, cuerpo.texto if cuerpo else None)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/admin/preguntas/{pregunta_id}/descartar", dependencies=[Depends(admin)])
    def descartar(pregunta_id: int):
        s.preguntas.descartar(pregunta_id)
        return {"id": pregunta_id, "estado": "descartada"}

    @app.get("/admin/ordenes", dependencies=[Depends(admin)])
    def listar_ordenes():
        return ordenes.listar(s.config)

    @app.get("/admin/ordenes/exportar", dependencies=[Depends(admin)])
    def exportar(formato: str = "xlsx"):
        if formato not in ("xlsx", "csv"):
            raise HTTPException(400, "formato debe ser xlsx o csv")
        ruta = ordenes.exportar(s.config, formato)
        return FileResponse(ruta, filename=ruta.name)

    return app


def __getattr__(nombre: str):
    # `uvicorn app.main:app` crea la app recién cuando se pide (así importar el módulo en las
    # pruebas no lee el .env ni crea la base real).
    if nombre == "app":
        global app
        app = crear_app()
        return app
    raise AttributeError(nombre)
