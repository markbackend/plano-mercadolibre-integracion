"""Simulador local de Mercado Libre (para pruebas y demo, sin internet ni cuentas).

Imita, de forma simplificada, las partes de la API que usa este proyecto:
OAuth (/authorization, /oauth/token con refresh_token que rota), /users/me, búsqueda de ítems,
multiget /items, GET/PUT /items/{id} (con variaciones), preguntas, respuestas y órdenes.
También puede devolver 429 a propósito para probar los reintentos.

Se usa de dos formas:
- En proceso, como transporte de httpx: `httpx.Client(transport=httpx.MockTransport(sim.manejar))`.
- Como servidor HTTP: `python -m app.simulador_servidor` (ver README).

No es una copia exacta de Mercado Libre: los formatos están basados en la documentación pública
y hay que verificarlos contra la API real al adaptar el plano a un cliente.
"""
from __future__ import annotations

import copy
import itertools
import json
import secrets
import threading
import time
from typing import Callable
from urllib.parse import parse_qs, urlencode

import httpx

USUARIO_DEMO = {"id": 123456789, "nickname": "VENDEDOR_DEMO", "site_id": "MLA"}
CLIENT_ID_DEMO = "1234567890123456"
CLIENT_SECRET_DEMO = "secreto-demo"


def _sku(valor):
    return [{"id": "SELLER_SKU", "name": "SKU", "value_name": valor}]


def items_demo() -> dict[str, dict]:
    items = [
        {"id": "MLA1000000001", "title": "Auriculares Bluetooth X200", "price": 25999.0, "currency_id": "ARS",
         "available_quantity": 10, "status": "active", "condition": "new", "listing_type_id": "gold_special",
         "seller_custom_field": None, "attributes": _sku("AUR-X200") + [
             {"id": "BRAND", "name": "Marca", "value_name": "SonidoPro"},
             {"id": "BATTERY_LIFE", "name": "Duración de la batería", "value_name": "20 h"}],
         "shipping": {"free_shipping": True}, "variations": []},
        {"id": "MLA1000000005", "title": "Auriculares Bluetooth X200 - 6 cuotas", "price": 28999.0,
         "currency_id": "ARS", "available_quantity": 10, "status": "active", "condition": "new",
         "listing_type_id": "gold_pro", "seller_custom_field": "AUR-X200", "attributes": [],
         "shipping": {"free_shipping": True}, "variations": []},
        {"id": "MLA1000000002", "title": "Remera de algodón lisa", "price": 8500.0, "currency_id": "ARS",
         "available_quantity": 8, "status": "active", "condition": "new", "listing_type_id": "gold_special",
         "seller_custom_field": None, "attributes": [], "shipping": {"free_shipping": False},
         "variations": [
             {"id": 9001, "price": 8500.0, "available_quantity": 5, "seller_custom_field": None,
              "attribute_combinations": [{"id": "COLOR", "value_name": "Negro"}, {"id": "SIZE", "value_name": "M"}],
              "attributes": _sku("REM-NEG-M")},
             {"id": 9002, "price": 8500.0, "available_quantity": 3, "seller_custom_field": "REM-NEG-L",
              "attribute_combinations": [{"id": "COLOR", "value_name": "Negro"}, {"id": "SIZE", "value_name": "L"}],
              "attributes": []},
             {"id": 9003, "price": 8500.0, "available_quantity": 0, "seller_custom_field": None,
              "attribute_combinations": [{"id": "COLOR", "value_name": "Blanco"}, {"id": "SIZE", "value_name": "M"}],
              "attributes": _sku("REM-BLA-M")},
         ]},
        {"id": "MLA1000000003", "title": "Cargador USB-C 20W", "price": 7999.0, "currency_id": "ARS",
         "available_quantity": 2, "status": "active", "condition": "new", "listing_type_id": "gold_special",
         "seller_custom_field": "CARG-20W", "attributes": [], "shipping": {"free_shipping": False},
         "variations": []},
        {"id": "MLA1000000004", "title": "Mate de calabaza", "price": 4500.0, "currency_id": "ARS",
         "available_quantity": 0, "status": "closed", "condition": "new", "listing_type_id": "gold_special",
         "seller_custom_field": "MATE-01", "attributes": [], "shipping": {"free_shipping": False},
         "variations": []},
    ]
    for it in items:
        it["seller_id"] = USUARIO_DEMO["id"]
    return {it["id"]: it for it in items}


def _json(status: int, datos, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=datos, headers=headers or {})


def _sku_de(obj: dict) -> str | None:
    if obj.get("seller_custom_field"):
        return obj["seller_custom_field"]
    for a in obj.get("attributes") or []:
        if a.get("id") == "SELLER_SKU":
            return a.get("value_name")
    return None


class SimuladorML:
    def __init__(self, reloj: Callable[[], float] = time.time, expires_in: int = 21600):
        self.reloj = reloj
        self.expires_in = expires_in
        self.lock = threading.RLock()
        self.items = items_demo()
        self.preguntas: dict[int, dict] = {}
        self.ordenes: dict[int, dict] = {}
        self.codigos: dict[str, str] = {}           # code -> redirect_uri
        self.access: dict[str, float] = {}          # access_token -> vence
        self.refresh_validos: set[str] = set()      # refresh_tokens que todavía sirven (rotan)
        self.refresh_usados = 0
        self.responder_429 = 0                      # próximos N pedidos devuelven 429
        self.registro: list[tuple[str, str]] = []   # (método, ruta) de cada pedido
        self.puts: list[tuple[str, dict]] = []
        self._ids_preg = itertools.count(5000000001)
        self._ids_ord = itertools.count(2000000001)

    # ---------- utilidades para pruebas/demo ----------
    def emitir_code(self, redirect_uri: str = "http://localhost:8000/auth/callback") -> str:
        code = "TG-" + secrets.token_hex(8)
        self.codigos[code] = redirect_uri
        return code

    def nueva_pregunta(self, item_id: str, texto: str) -> dict:
        with self.lock:
            pid = next(self._ids_preg)
            self.preguntas[pid] = {"id": pid, "item_id": item_id, "seller_id": USUARIO_DEMO["id"],
                                   "text": texto, "status": "UNANSWERED", "answer": None,
                                   "date_created": "2026-11-18T10:00:00.000-03:00", "from": {"id": 999}}
            return self._notif("questions", f"/questions/{pid}")

    def nueva_orden(self, item_id: str, cantidad: int = 1, variacion_id: int | None = None,
                    estado: str = "paid") -> dict:
        with self.lock:
            it = self.items[item_id]
            oid = next(self._ids_ord)
            if variacion_id:
                v = next(v for v in it["variations"] if v["id"] == variacion_id)
                v["available_quantity"] = max(0, v["available_quantity"] - cantidad)
                sku = _sku_de(v)
            else:
                it["available_quantity"] = max(0, it["available_quantity"] - cantidad)
                sku = _sku_de(it)
            self.ordenes[oid] = {
                "id": oid, "status": estado, "date_created": "2026-11-18T11:00:00.000-03:00",
                "total_amount": it["price"] * cantidad, "currency_id": it["currency_id"],
                "buyer": {"id": 555, "nickname": "COMPRADOR_TEST"},
                "order_items": [{"item": {"id": item_id, "title": it["title"], "seller_sku": sku,
                                          "variation_id": variacion_id},
                                 "quantity": cantidad, "unit_price": it["price"]}],
            }
            return self._notif("orders_v2", f"/orders/{oid}")

    def _notif(self, topic: str, resource: str) -> dict:
        return {"_id": secrets.token_hex(12), "resource": resource, "user_id": USUARIO_DEMO["id"],
                "topic": topic, "application_id": int(CLIENT_ID_DEMO), "attempts": 1,
                "sent": "2026-11-18T11:00:00.000Z", "received": "2026-11-18T11:00:00.000Z"}

    # ---------- transporte ----------
    def manejar(self, req: httpx.Request) -> httpx.Response:
        with self.lock:
            ruta = req.url.path
            self.registro.append((req.method, ruta))
            if self.responder_429 > 0:
                self.responder_429 -= 1
                return _json(429, {"message": "Too Many Requests", "error": "local_rate_limited"},
                             {"Retry-After": "1"})
            if ruta == "/authorization":
                return self._autorizacion(req)
            if ruta == "/oauth/token" and req.method == "POST":
                return self._token(req)
            if not self._autenticado(req):
                return _json(401, {"message": "invalid access token", "error": "not_found", "status": 401})
            return self._api(req, ruta)

    def _autorizacion(self, req: httpx.Request) -> httpx.Response:
        p = req.url.params
        if p.get("client_id") != CLIENT_ID_DEMO:
            return _json(400, {"error": "invalid_client"})
        code = self.emitir_code(p.get("redirect_uri", ""))
        destino = f"{p.get('redirect_uri')}?{urlencode({'code': code, 'state': p.get('state', '')})}"
        return httpx.Response(302, headers={"Location": destino})

    def _token(self, req: httpx.Request) -> httpx.Response:
        f = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        if f.get("client_id") != CLIENT_ID_DEMO or f.get("client_secret") != CLIENT_SECRET_DEMO:
            return _json(401, {"error": "invalid_client", "message": "invalid client_id or client_secret"})
        tipo = f.get("grant_type")
        if tipo == "authorization_code":
            if self.codigos.pop(f.get("code", ""), None) is None:
                return _json(400, {"error": "invalid_grant", "message": "Error validating grant."})
        elif tipo == "refresh_token":
            rt = f.get("refresh_token", "")
            if rt not in self.refresh_validos:
                return _json(400, {"error": "invalid_grant", "message": "Error validating grant."})
            self.refresh_validos.discard(rt)  # el refresh_token rota: el viejo ya no sirve
            self.refresh_usados += 1
        else:
            return _json(400, {"error": "unsupported_grant_type"})
        access = "APP_USR-" + secrets.token_hex(12)
        refresh = "TG-" + secrets.token_hex(12)
        self.access[access] = self.reloj() + self.expires_in
        self.refresh_validos.add(refresh)
        return _json(200, {"access_token": access, "token_type": "Bearer", "expires_in": self.expires_in,
                           "scope": "offline_access read write", "user_id": USUARIO_DEMO["id"],
                           "refresh_token": refresh})

    def _autenticado(self, req: httpx.Request) -> bool:
        h = req.headers.get("Authorization", "")
        if not h.startswith("Bearer "):
            return False
        vence = self.access.get(h[7:])
        return vence is not None and vence > self.reloj()

    def _api(self, req: httpx.Request, ruta: str) -> httpx.Response:
        p = req.url.params
        partes = ruta.strip("/").split("/")
        m = req.method
        if m == "GET" and ruta == "/users/me":
            return _json(200, USUARIO_DEMO)
        if m == "GET" and len(partes) == 4 and partes[0] == "users" and partes[2] == "items" and partes[3] == "search":
            ids = [i for i, it in self.items.items() if str(it["seller_id"]) == partes[1]]
            if p.get("seller_sku"):
                ids = [i for i in ids if p["seller_sku"] in self._skus(self.items[i])]
            off, lim = int(p.get("offset", 0)), int(p.get("limit", 50))
            return _json(200, {"seller_id": partes[1], "results": ids[off:off + lim],
                               "paging": {"total": len(ids), "offset": off, "limit": lim}})
        if m == "GET" and ruta == "/items":
            salida = []
            for i in (p.get("ids") or "").split(","):
                if i in self.items:
                    salida.append({"code": 200, "body": copy.deepcopy(self.items[i])})
                else:
                    salida.append({"code": 404, "body": {"error": "not_found", "message": f"Item {i} not found"}})
            return _json(200, salida)
        if len(partes) == 2 and partes[0] == "items":
            it = self.items.get(partes[1])
            if not it:
                return _json(404, {"error": "not_found", "message": "Item not found"})
            if m == "GET":
                return _json(200, copy.deepcopy(it))
            if m == "PUT":
                return self._put_item(it, json.loads(req.content or b"{}"))
        if len(partes) == 2 and partes[0] == "questions" and m == "GET":
            if partes[1] == "search":
                qs = [q for q in self.preguntas.values()
                      if str(q["seller_id"]) == p.get("seller_id") and (not p.get("status") or q["status"] == p["status"])]
                return _json(200, {"questions": qs, "total": len(qs)})
            q = self.preguntas.get(int(partes[1])) if partes[1].isdigit() else None
            return _json(200, q) if q else _json(404, {"error": "not_found"})
        if ruta == "/answers" and m == "POST":
            d = json.loads(req.content or b"{}")
            q = self.preguntas.get(int(d.get("question_id", 0)))
            if not q:
                return _json(404, {"error": "not_found"})
            if q["status"] != "UNANSWERED":
                return _json(400, {"error": "question_already_answered"})
            texto = (d.get("text") or "").strip()
            if not texto or len(texto) > 2000:
                return _json(400, {"error": "invalid_text"})
            q["status"] = "ANSWERED"
            q["answer"] = {"text": texto, "status": "ACTIVE", "date_created": "2026-11-18T10:05:00.000-03:00"}
            return _json(200, q)
        if len(partes) == 2 and partes[0] == "orders" and m == "GET":
            o = self.ordenes.get(int(partes[1])) if partes[1].isdigit() else None
            return _json(200, o) if o else _json(404, {"error": "not_found"})
        return _json(404, {"error": "not_found", "message": f"Ruta no simulada: {m} {ruta}"})

    def _skus(self, it: dict) -> set[str]:
        s = {_sku_de(it)} | {_sku_de(v) for v in it.get("variations") or []}
        return {x for x in s if x}

    def _put_item(self, it: dict, cambios: dict) -> httpx.Response:
        if it["status"] == "closed":
            return _json(400, {"error": "item.status.invalid", "message": "Item is closed"})
        nuevo = copy.deepcopy(it)
        if "price" in cambios:
            if not isinstance(cambios["price"], (int, float)) or cambios["price"] <= 0:
                return _json(400, {"error": "item.price.invalid", "message": "Invalid price"})
            nuevo["price"] = float(cambios["price"])
        if "available_quantity" in cambios:
            q = cambios["available_quantity"]
            if not isinstance(q, int) or q < 0:
                return _json(400, {"error": "item.available_quantity.invalid"})
            nuevo["available_quantity"] = q
        if "variations" in cambios:
            existentes = {v["id"]: v for v in nuevo["variations"]}
            lista = []
            for cv in cambios["variations"]:
                v = existentes.get(cv.get("id"))
                if v is None:
                    return _json(400, {"error": "item.variations.invalid", "message": "Unknown variation"})
                v = dict(v)
                if "available_quantity" in cv:
                    if not isinstance(cv["available_quantity"], int) or cv["available_quantity"] < 0:
                        return _json(400, {"error": "item.variations.available_quantity.invalid"})
                    v["available_quantity"] = cv["available_quantity"]
                if "price" in cv:
                    v["price"] = float(cv["price"])
                lista.append(v)
            # Igual que en Mercado Libre: las variaciones que no se mandan se eliminan.
            if len({v["price"] for v in lista}) > 1:
                return _json(400, {"error": "item.variations.price.invalid",
                                   "message": "All variations must have the same price"})
            nuevo["variations"] = lista
            nuevo["available_quantity"] = sum(v["available_quantity"] for v in lista)
            if lista:
                nuevo["price"] = lista[0]["price"]
        self.items[it["id"]] = nuevo
        self.puts.append((it["id"], cambios))
        return _json(200, copy.deepcopy(nuevo))


class TransporteDemo(httpx.BaseTransport):
    """Transporte que manda Mercado Libre al simulador y responde también por la IA, Telegram y
    el webhook de avisos con versiones falsas. Nada sale a internet."""

    def __init__(self, sim: SimuladorML, ia: Callable[[httpx.Request], httpx.Response] | None = None,
                 extra: dict[str, Callable[[httpx.Request], httpx.Response]] | None = None):
        self.sim = sim
        self.ia = ia or ia_falsa
        self.extra = extra or {}
        self.avisos: list[dict] = []

    def handle_request(self, req: httpx.Request) -> httpx.Response:
        host = req.url.host
        if host in self.extra:
            return self.extra[host](req)
        if host == "api.anthropic.com":
            return self.ia(req)
        if host == "api.telegram.org" or host == "avisos.local":
            self.avisos.append({"host": host, "cuerpo": json.loads(req.content or b"{}")})
            return _json(200, {"ok": True})
        return self.sim.manejar(req)


def ia_falsa(req: httpx.Request) -> httpx.Response:
    """Imita la API de mensajes de Anthropic: responde solo si el dato está en la publicación."""
    cuerpo = json.loads(req.content)
    texto = cuerpo["messages"][0]["content"]
    pregunta = texto.split("PREGUNTA DEL COMPRADOR:")[-1].lower()
    if "bateria" in pregunta or "batería" in pregunta:
        if "Duración de la batería" in texto:
            respuesta = "Hola! Según la ficha del producto, la batería dura 20 h. Saludos."
        else:
            respuesta = "SIN_DATOS"
    else:
        respuesta = "SIN_DATOS"
    return _json(200, {"id": "msg_demo", "type": "message", "role": "assistant", "model": cuerpo["model"],
                       "content": [{"type": "text", "text": respuesta}], "stop_reason": "end_turn",
                       "usage": {"input_tokens": 100, "output_tokens": 20}})
