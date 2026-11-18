"""Notificaciones (webhook) de Mercado Libre: orders_v2, questions e items.

Mercado Libre espera un 200 rápido (si tarda o falla, reintenta y puede deshabilitar el webhook).
Por eso el endpoint solo valida y encola en SQLite; el procesamiento (consultar el recurso a la API
y actuar) se hace después, en segundo plano o con `python -m app.cli procesar`.

Duplicados:
- Misma notificación reenviada (mismo `_id`) -> se ignora (clave única).
- Varias notificaciones del mismo recurso mientras una está pendiente -> se agrupan en una sola,
  porque al procesar siempre se lee el estado actual del recurso.
- El procesamiento es idempotente (órdenes por id, preguntas ya respondidas no se tocan).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from . import ordenes
from .config import Config
from .db import conectar
from .servicios import Servicios

log = logging.getLogger("ml.notificaciones")

TOPICOS = {"orders_v2", "questions", "items"}
MAX_INTENTOS = 5
MINUTOS_TRABADA = 10


def registrar(config: Config, cuerpo: dict) -> tuple[str, int | None]:
    """Encola la notificación. Devuelve (resultado, id): encolada | duplicada | ignorada."""
    topic = str(cuerpo.get("topic") or "")
    resource = str(cuerpo.get("resource") or "")
    if not topic or not resource:
        log.warning("Notificación sin topic o resource: %s", cuerpo)
        return "ignorada", None
    if topic not in TOPICOS:
        log.info("Tópico %s no manejado; se ignora", topic)
        return "ignorada", None
    app_id = cuerpo.get("application_id")
    if config.client_id and app_id is not None and str(app_id) != str(config.client_id):
        log.warning("Notificación de otra aplicación (%s); se ignora", app_id)
        return "ignorada", None

    clave = str(cuerpo.get("_id") or hashlib.sha1(
        f"{topic}|{resource}|{cuerpo.get('sent', '')}".encode()).hexdigest())
    con = conectar(config.db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM notificaciones WHERE clave=?", (clave,)).fetchone():
            con.execute("COMMIT")
            return "duplicada", None
        pendiente = con.execute(
            "SELECT id FROM notificaciones WHERE topic=? AND resource=? AND estado='pendiente'",
            (topic, resource)).fetchone()
        if pendiente:
            con.execute("COMMIT")
            return "duplicada", pendiente["id"]
        cur = con.execute(
            "INSERT INTO notificaciones(clave, topic, resource, user_id, cuerpo) VALUES (?,?,?,?,?)",
            (clave, topic, resource, cuerpo.get("user_id"), json.dumps(cuerpo, ensure_ascii=False)))
        con.execute("COMMIT")
        return "encolada", cur.lastrowid
    except Exception:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def _tomar(con, nid: int) -> bool:
    """Reserva una notificación para procesarla (evita que dos workers hagan la misma)."""
    cur = con.execute(
        "UPDATE notificaciones SET estado='procesando', intentos=intentos+1, tomado=datetime('now') "
        "WHERE id=? AND estado IN ('pendiente','error') AND intentos < ?", (nid, MAX_INTENTOS))
    return cur.rowcount == 1


def _recuperar_trabadas(con) -> int:
    """Si un proceso se cayó a mitad de camino, la notificación queda en 'procesando' para siempre.
    Las que llevan más de MINUTOS_TRABADA así vuelven a 'error' para reintentarse."""
    cur = con.execute(
        "UPDATE notificaciones SET estado='error', error='quedó trabada procesando; se reintenta' "
        "WHERE estado='procesando' AND tomado < datetime('now', ?)", (f"-{MINUTOS_TRABADA} minutes",))
    if cur.rowcount:
        log.warning("%d notificaciones trabadas vuelven a la cola", cur.rowcount)
    return cur.rowcount


def procesar_pendientes(s: Servicios, limite: int = 100) -> dict:
    con = conectar(s.config.db_path)
    try:
        _recuperar_trabadas(con)
        filas = con.execute(
            "SELECT id, topic, resource FROM notificaciones "
            "WHERE estado IN ('pendiente','error') AND intentos < ? ORDER BY id LIMIT ?",
            (MAX_INTENTOS, limite)).fetchall()
        resumen = {"ok": 0, "error": 0}
        for f in filas:
            if not _tomar(con, f["id"]):
                continue
            try:
                detalle = procesar_una(s, f["topic"], f["resource"])
                con.execute("UPDATE notificaciones SET estado='ok', error=NULL, procesado=datetime('now') "
                            "WHERE id=?", (f["id"],))
                resumen["ok"] += 1
                log.info("Notificación %s (%s %s) procesada: %s", f["id"], f["topic"], f["resource"], detalle)
            except Exception as e:  # se registra y se reintenta en la próxima pasada
                con.execute("UPDATE notificaciones SET estado='error', error=? WHERE id=?", (str(e)[:500], f["id"]))
                resumen["error"] += 1
                log.exception("Error procesando notificación %s (%s %s)", f["id"], f["topic"], f["resource"])
        return resumen
    finally:
        con.close()


def _id_de(resource: str) -> str:
    m = re.search(r"/(?:orders|questions|items)/([A-Za-z0-9]+)", resource)
    if not m:
        raise ValueError(f"No se reconoce el recurso {resource}")
    return m.group(1)


def procesar_una(s: Servicios, topic: str, resource: str) -> dict:
    if topic == "orders_v2":
        return ordenes.procesar_orden(s.config, s.ml, s.http, int(_id_de(resource)))
    if topic == "questions":
        return s.preguntas.procesar(int(_id_de(resource)))
    if topic == "items":
        item = s.ml.item(_id_de(resource))
        con = conectar(s.config.db_path)
        try:
            con.execute(
                """INSERT INTO items_cache(id, titulo, precio, stock, estado) VALUES (?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET titulo=excluded.titulo, precio=excluded.precio,
                     stock=excluded.stock, estado=excluded.estado, actualizado=datetime('now')""",
                (item["id"], item.get("title"), item.get("price"), item.get("available_quantity"),
                 item.get("status")))
        finally:
            con.close()
        return {"item": item["id"], "precio": item.get("price"), "stock": item.get("available_quantity"),
                "estado": item.get("status")}
    raise ValueError(f"Tópico no manejado: {topic}")


def estado_cola(config: Config) -> dict:
    con = conectar(config.db_path)
    try:
        return {f["estado"]: f["n"] for f in
                con.execute("SELECT estado, COUNT(*) n FROM notificaciones GROUP BY estado")}
    finally:
        con.close()
