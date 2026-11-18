"""OAuth 2.0 de Mercado Libre.

Flujo:
1. `url_autorizacion()` arma el link al que entra el vendedor para dar permiso a la aplicación.
2. Mercado Libre redirige a ML_REDIRECT_URI con `?code=...&state=...`.
3. `intercambiar_code()` cambia el code por access_token + refresh_token y los guarda en SQLite.
4. `token_valido()` devuelve un access_token vigente; si está por vencer lo renueva con el
   refresh_token. El refresh_token ROTA en cada uso (el viejo deja de servir), por eso la renovación
   se hace dentro de una transacción `BEGIN IMMEDIATE` de SQLite: funciona como bloqueo entre
   procesos (servidor web, tarea programada, worker) y entre hilos. El que llega segundo vuelve a
   leer el token y, si ya lo renovó otro, usa ese en lugar de gastar el refresh_token viejo.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Callable
from urllib.parse import urlencode

import httpx

from .config import Config
from .db import conectar

log = logging.getLogger("ml.oauth")


class ErrorAutorizacion(Exception):
    """No hay token o el refresh_token ya no sirve: el vendedor tiene que volver a autorizar."""


class GestorTokens:
    def __init__(self, config: Config, http: httpx.Client, reloj: Callable[[], float] = time.time,
                 dormir: Callable[[float], None] = time.sleep):
        self.config = config
        self.http = http
        self.reloj = reloj
        self.dormir = dormir
        self._lock_hilos = threading.Lock()

    # ---------- Paso 1: link de autorización ----------
    def url_autorizacion(self, state: str | None = None) -> tuple[str, str]:
        state = state or secrets.token_urlsafe(16)
        con = conectar(self.config.db_path)
        try:
            con.execute("INSERT OR REPLACE INTO oauth_estados(state) VALUES (?)", (state,))
            # Limpia estados viejos (más de 1 día) para que la tabla no crezca.
            con.execute("DELETE FROM oauth_estados WHERE creado < datetime('now', '-1 day')")
        finally:
            con.close()
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "state": state,
        }
        return f"{self.config.dominio_auth}/authorization?{urlencode(params)}", state

    # ---------- Paso 2: code -> tokens ----------
    def intercambiar_code(self, code: str, state: str | None) -> dict:
        con = conectar(self.config.db_path)
        try:
            if state is not None:
                fila = con.execute("DELETE FROM oauth_estados WHERE state = ? RETURNING state", (state,)).fetchone()
                if fila is None:
                    raise ErrorAutorizacion("El parámetro state no coincide (posible CSRF o link vencido).")
        finally:
            con.close()
        datos = self._pedir_token({
            "grant_type": "authorization_code",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "code": code,
            "redirect_uri": self.config.redirect_uri,
        })
        con = conectar(self.config.db_path)
        try:
            con.execute("BEGIN IMMEDIATE")
            self._guardar(con, datos)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()
        log.info("Autorización completa para el usuario %s", datos.get("user_id"))
        return datos

    # ---------- Uso diario: token vigente ----------
    def token_valido(self) -> str:
        fila = self._leer()
        if fila is None:
            raise ErrorAutorizacion("No hay tokens guardados. Entrá a /auth/inicio para autorizar la aplicación.")
        if fila["expira_en"] - self.config.margen_renovacion_seg > self.reloj():
            return fila["access_token"]
        return self.renovar(token_vencido=fila["access_token"])

    def user_id(self) -> int | None:
        fila = self._leer()
        return fila["user_id"] if fila else None

    def renovar(self, token_vencido: str | None = None) -> str:
        """Renueva el access_token. Si otro proceso ya lo renovó mientras esperábamos, usa ese.

        `token_vencido`: el token que el que llama considera malo. Si el guardado es distinto y
        sigue vigente, no se renueva de nuevo (evita gastar dos veces el refresh_token).
        """
        with self._lock_hilos:
            con = conectar(self.config.db_path)
            try:
                con.execute("BEGIN IMMEDIATE")  # bloqueo de escritura: un solo renovador a la vez
                fila = con.execute("SELECT * FROM tokens WHERE id = 1").fetchone()
                if fila is None:
                    con.execute("ROLLBACK")
                    raise ErrorAutorizacion("No hay tokens guardados.")
                vigente = fila["expira_en"] - self.config.margen_renovacion_seg > self.reloj()
                if vigente and fila["access_token"] != token_vencido:
                    con.execute("COMMIT")
                    log.info("Otro proceso ya renovó el token; se usa el nuevo.")
                    return fila["access_token"]
                log.info("Renovando access_token con refresh_token...")
                datos = self._pedir_token({
                    "grant_type": "refresh_token",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "refresh_token": fila["refresh_token"],
                })
                self._guardar(con, datos, user_id_previo=fila["user_id"])
                con.execute("COMMIT")
                log.info("Token renovado; vence en %s segundos.", datos.get("expires_in"))
                return datos["access_token"]
            except Exception:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            finally:
                con.close()

    # ---------- internos ----------
    def _pedir_token(self, formulario: dict) -> dict:
        url = f"{self.config.api_url}/oauth/token"
        for intento in range(self.config.max_reintentos + 1):
            try:
                r = self.http.post(url, data=formulario, headers={"Accept": "application/json"})
            except httpx.HTTPError as e:
                raise ErrorAutorizacion(f"No se pudo contactar a Mercado Libre: {e}") from e
            # 429 = límite de pedidos: es seguro reintentar (el pedido no se procesó).
            if r.status_code != 429 or intento == self.config.max_reintentos:
                break
            try:
                espera = float(r.headers.get("Retry-After", ""))
            except ValueError:
                espera = self.config.espera_base_seg * (2 ** intento)
            log.warning("429 al pedir token; reintento en %.1fs", espera)
            self.dormir(espera)
        if r.status_code != 200:
            try:
                detalle = r.json()
            except ValueError:
                detalle = r.text
            if r.status_code in (400, 401, 403):
                raise ErrorAutorizacion(
                    f"Mercado Libre rechazó el pedido de token ({r.status_code}): {detalle}. "
                    "Si el refresh_token venció o fue usado, hay que volver a autorizar."
                )
            raise ErrorAutorizacion(f"Error {r.status_code} al pedir token: {detalle}")
        datos = r.json()
        for campo in ("access_token", "refresh_token", "expires_in"):
            if campo not in datos:
                raise ErrorAutorizacion(f"Respuesta de token incompleta: falta {campo}")
        return datos

    def _guardar(self, con, datos: dict, user_id_previo: int | None = None) -> None:
        expira = self.reloj() + float(datos["expires_in"])
        con.execute(
            """INSERT INTO tokens(id, user_id, access_token, refresh_token, expira_en, actualizado)
               VALUES (1, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id, access_token=excluded.access_token,
                   refresh_token=excluded.refresh_token, expira_en=excluded.expira_en,
                   actualizado=excluded.actualizado""",
            (datos.get("user_id", user_id_previo), datos["access_token"], datos["refresh_token"], expira),
        )

    def _leer(self):
        con = conectar(self.config.db_path)
        try:
            return con.execute("SELECT * FROM tokens WHERE id = 1").fetchone()
        finally:
            con.close()
