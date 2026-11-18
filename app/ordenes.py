"""Órdenes: guardar las nuevas, avisar al dueño una sola vez y exportar a Excel/CSV."""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path

import httpx

from .avisos import avisar
from .cliente_ml import ClienteML
from .config import Config
from .db import conectar

log = logging.getLogger("ml.ordenes")

COLUMNAS = ["id", "fecha", "estado", "total", "moneda", "comprador", "productos"]


def _resumen_items(orden: dict) -> list[dict]:
    salida = []
    for oi in orden.get("order_items") or []:
        it = oi.get("item") or {}
        salida.append({
            "item_id": it.get("id"),
            "titulo": it.get("title"),
            "sku": it.get("seller_sku") or it.get("seller_custom_field"),
            "variacion_id": it.get("variation_id"),
            "cantidad": oi.get("quantity"),
            "precio_unitario": oi.get("unit_price"),
        })
    return salida


def guardar_orden(config: Config, orden: dict) -> bool:
    """Guarda o actualiza la orden. Devuelve True si es nueva."""
    items = _resumen_items(orden)
    comprador = (orden.get("buyer") or {}).get("nickname") or str((orden.get("buyer") or {}).get("id", ""))
    con = conectar(config.db_path)
    try:
        existente = con.execute("SELECT id FROM ordenes WHERE id=?", (orden["id"],)).fetchone()
        con.execute(
            """INSERT INTO ordenes(id, estado, fecha, total, moneda, comprador, items)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET estado=excluded.estado, total=excluded.total,
                 items=excluded.items, actualizado=datetime('now')""",
            (orden["id"], orden.get("status"), orden.get("date_created"), orden.get("total_amount"),
             orden.get("currency_id"), comprador, json.dumps(items, ensure_ascii=False)))
        return existente is None
    finally:
        con.close()


def procesar_orden(config: Config, ml: ClienteML, http_avisos: httpx.Client, orden_id: int) -> dict:
    orden = ml.orden(orden_id)
    nueva = guardar_orden(config, orden)
    avisada = _marcar_para_aviso(config, orden["id"], orden.get("status"))
    if avisada:
        productos = ", ".join(f"{i['cantidad']} x {i['titulo']}" for i in _resumen_items(orden))
        texto = (f"Nueva venta en Mercado Libre: orden {orden['id']} - {productos} - "
                 f"total {orden.get('currency_id', '')} {orden.get('total_amount')} - "
                 f"comprador {(orden.get('buyer') or {}).get('nickname', '?')}")
        canales = avisar(config, http_avisos, texto, {"orden_id": orden["id"], "estado": orden.get("status")})
        hay_canales = bool(config.aviso_webhook_url or (config.telegram_bot_token and config.telegram_chat_id))
        if hay_canales and not canales:
            # Fallaron todos los canales: se desmarca para que el reintento de la notificación vuelva a avisar.
            _desmarcar_aviso(config, orden["id"])
            raise RuntimeError(f"No se pudo avisar la orden {orden['id']} por ningún canal; se reintentará")
        log.info("Orden %s avisada por %s", orden["id"], canales or "log")
    return {"id": orden["id"], "nueva": nueva, "avisada": avisada, "estado": orden.get("status")}


def _marcar_para_aviso(config: Config, orden_id: int, estado: str | None) -> bool:
    """Marca la orden como avisada de forma atómica. Solo avisa órdenes pagadas, y una sola vez."""
    if estado != "paid":
        return False
    con = conectar(config.db_path)
    try:
        cur = con.execute("UPDATE ordenes SET avisada=1 WHERE id=? AND avisada=0", (orden_id,))
        return cur.rowcount == 1
    finally:
        con.close()


def _desmarcar_aviso(config: Config, orden_id: int) -> None:
    con = conectar(config.db_path)
    try:
        con.execute("UPDATE ordenes SET avisada=0 WHERE id=?", (orden_id,))
    finally:
        con.close()


def listar(config: Config) -> list[dict]:
    con = conectar(config.db_path)
    try:
        filas = [dict(f) for f in con.execute("SELECT * FROM ordenes ORDER BY fecha DESC, id DESC")]
    finally:
        con.close()
    for f in filas:
        f["items"] = json.loads(f["items"] or "[]")
    return filas


def _filas_export(config: Config) -> list[list]:
    salida = []
    for o in listar(config):
        productos = "; ".join(f"{i['cantidad']} x {i['titulo']} (SKU {i.get('sku') or '-'})" for i in o["items"])
        salida.append([o["id"], o["fecha"], o["estado"], o["total"], o["moneda"], o["comprador"], productos])
    return salida


def exportar(config: Config, formato: str = "xlsx", ruta: str | None = None) -> Path:
    if formato not in ("xlsx", "csv"):
        raise ValueError("Formato debe ser xlsx o csv")
    Path(config.salidas_dir).mkdir(parents=True, exist_ok=True)
    destino = Path(ruta or Path(config.salidas_dir) / f"ordenes_{datetime.now():%Y%m%d_%H%M%S}.{formato}")
    filas = _filas_export(config)
    if formato == "csv":
        with destino.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")  # ; para que Excel en español lo abra bien
            w.writerow(COLUMNAS)
            w.writerows(filas)
    else:
        from openpyxl import Workbook

        libro = Workbook()
        hoja = libro.active
        hoja.title = "Órdenes"
        hoja.append(COLUMNAS)
        for f in filas:
            hoja.append(f)
        for col, ancho in zip("ABCDEFG", (16, 26, 12, 12, 8, 20, 60)):
            hoja.column_dimensions[col].width = ancho
        libro.save(destino)
    log.info("Exportadas %d órdenes a %s", len(filas), destino)
    return destino
