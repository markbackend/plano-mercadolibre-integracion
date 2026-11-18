"""Cliente de la API REST de Mercado Libre con httpx (sin SDK).

- Agrega el access_token vigente en cada pedido (lo renueva solo si hace falta).
- Ante 401 renueva el token una vez y reintenta.
- Ante 429 (demasiados pedidos) y errores 5xx espera y reintenta: respeta el encabezado
  Retry-After si viene; si no, espera exponencial (1s, 2s, 4s...).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from .config import Config
from .oauth import GestorTokens

log = logging.getLogger("ml.api")

REINTENTABLES = {429, 500, 502, 503, 504}


class ErrorML(Exception):
    def __init__(self, status: int, metodo: str, ruta: str, detalle: Any):
        self.status = status
        self.detalle = detalle
        super().__init__(f"{metodo} {ruta} -> {status}: {detalle}")


class ClienteML:
    def __init__(self, config: Config, tokens: GestorTokens, http: httpx.Client,
                 dormir: Callable[[float], None] = time.sleep):
        self.config = config
        self.tokens = tokens
        self.http = http
        self.dormir = dormir

    # ---------- pedido genérico ----------
    def pedir(self, metodo: str, ruta: str, *, params: dict | None = None, json: Any = None) -> Any:
        url = f"{self.config.api_url}{ruta}"
        renovado = False
        intento = 0
        while True:
            token = self.tokens.token_valido()
            try:
                r = self.http.request(metodo, url, params=params, json=json,
                                      headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            except httpx.TransportError as e:
                if intento >= self.config.max_reintentos:
                    raise ErrorML(0, metodo, ruta, f"error de red: {e}") from e
                espera = self._espera(intento, None)
                log.warning("Error de red en %s %s (%s); reintento en %.1fs", metodo, ruta, e, espera)
                self.dormir(espera)
                intento += 1
                continue

            if r.status_code == 401 and not renovado:
                log.info("401 en %s %s: se renueva el token y se reintenta", metodo, ruta)
                self.tokens.renovar(token_vencido=token)
                renovado = True
                continue

            if r.status_code in REINTENTABLES and intento < self.config.max_reintentos:
                espera = self._espera(intento, r.headers.get("Retry-After"))
                log.warning("%s en %s %s; reintento %d/%d en %.1fs", r.status_code, metodo, ruta,
                            intento + 1, self.config.max_reintentos, espera)
                self.dormir(espera)
                intento += 1
                continue

            if r.status_code >= 400:
                try:
                    detalle = r.json()
                except ValueError:
                    detalle = r.text
                raise ErrorML(r.status_code, metodo, ruta, detalle)

            if not r.content:
                return None
            return r.json()

    def _espera(self, intento: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return self.config.espera_base_seg * (2 ** intento)

    # ---------- atajos de la API ----------
    def yo(self) -> dict:
        return self.pedir("GET", "/users/me")

    def buscar_items_vendedor(self, user_id: int, seller_sku: str | None = None) -> list[str]:
        """IDs de las publicaciones del vendedor (todas, paginando). Con seller_sku filtra por SKU."""
        ids: list[str] = []
        offset, limite = 0, 50
        while True:
            params = {"offset": offset, "limit": limite}
            if seller_sku:
                params["seller_sku"] = seller_sku
            datos = self.pedir("GET", f"/users/{user_id}/items/search", params=params)
            resultados = datos.get("results", [])
            ids.extend(resultados)
            total = datos.get("paging", {}).get("total", len(ids))
            offset += limite
            # Nota: más de 1000 publicaciones requieren search_type=scan (ver README).
            if not resultados or offset >= total or offset >= 1000:
                return ids

    def items(self, ids: list[str]) -> list[dict]:
        """Multiget: hasta 20 ítems por pedido, con atributos de las variaciones."""
        salida: list[dict] = []
        for i in range(0, len(ids), 20):
            lote = ids[i:i + 20]
            datos = self.pedir("GET", "/items", params={"ids": ",".join(lote), "include_attributes": "all"})
            for d in datos:
                if d.get("code") == 200:
                    salida.append(d["body"])
                else:
                    log.warning("No se pudo leer el ítem: %s", d)
        return salida

    def item(self, item_id: str) -> dict:
        return self.pedir("GET", f"/items/{item_id}", params={"include_attributes": "all"})

    def actualizar_item(self, item_id: str, cambios: dict) -> dict:
        return self.pedir("PUT", f"/items/{item_id}", json=cambios)

    def pregunta(self, pregunta_id: int) -> dict:
        return self.pedir("GET", f"/questions/{pregunta_id}")

    def responder(self, pregunta_id: int, texto: str) -> dict:
        return self.pedir("POST", "/answers", json={"question_id": pregunta_id, "text": texto})

    def preguntas_sin_responder(self, seller_id: int) -> list[dict]:
        datos = self.pedir("GET", "/questions/search",
                           params={"seller_id": seller_id, "status": "UNANSWERED", "api_version": 4})
        return datos.get("questions", [])

    def orden(self, orden_id: int) -> dict:
        return self.pedir("GET", f"/orders/{orden_id}")

    def recurso(self, resource: str) -> dict:
        """Lee el recurso que llega en una notificación (por ejemplo '/orders/123')."""
        return self.pedir("GET", resource)
