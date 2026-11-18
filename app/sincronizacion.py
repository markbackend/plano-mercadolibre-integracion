"""Sincronización de stock y precio: fuente del cliente -> publicaciones de Mercado Libre.

Cómo se busca el SKU en Mercado Libre:
- Publicación sin variaciones: campo `seller_custom_field` o atributo `SELLER_SKU`.
- Publicación con variaciones: lo mismo, pero en cada variación.
Un mismo SKU puede estar en varias publicaciones (por ejemplo, clásica y premium): se actualizan todas.

Modo simulacro (por defecto): calcula y muestra qué cambiaría, sin hacer ningún PUT.

IMPORTANTE con variaciones: al hacer PUT /items/{id} con "variations", Mercado Libre interpreta que
las variaciones que NO vienen en la lista se eliminan. Por eso siempre se mandan TODAS las
variaciones (las que no cambian van solo con su id). Además, todas las variaciones de una
publicación comparten precio: si la fuente da precios distintos, se informa como error.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from .cliente_ml import ClienteML, ErrorML
from .config import Config
from .db import conectar
from .fuente import FilaFuente

log = logging.getLogger("ml.sync")

ESTADOS_SINCRONIZABLES = {"active", "paused"}


def sku_de(objeto: dict) -> str | None:
    """SKU de un ítem o de una variación."""
    if objeto.get("seller_custom_field"):
        return str(objeto["seller_custom_field"]).strip()
    for clave in ("attributes", "attribute_combinations"):
        for a in objeto.get(clave) or []:
            if a.get("id") == "SELLER_SKU" and a.get("value_name"):
                return str(a["value_name"]).strip()
    return None


@dataclass
class Cambio:
    sku: str
    item_id: str
    variacion_id: int | None
    campo: str  # stock | precio
    antes: float | int | None
    despues: float | int


@dataclass
class Reporte:
    ejecucion: str
    simulacro: bool
    cambios: list[Cambio] = field(default_factory=list)
    items_actualizados: list[str] = field(default_factory=list)
    sin_cambios: int = 0
    no_encontrados: list[str] = field(default_factory=list)
    errores: list[str] = field(default_factory=list)

    def resumen(self) -> str:
        modo = "SIMULACRO (no se tocó nada)" if self.simulacro else "REAL"
        lineas = [f"Sincronización {modo} - ejecución {self.ejecucion}",
                  f"  Cambios: {len(self.cambios)} en {len({c.item_id for c in self.cambios})} publicaciones",
                  f"  SKUs sin cambios: {self.sin_cambios}",
                  f"  SKUs que no están en Mercado Libre: {len(self.no_encontrados)}"]
        for c in self.cambios:
            var = f" variación {c.variacion_id}" if c.variacion_id else ""
            lineas.append(f"    - {c.sku} ({c.item_id}{var}) {c.campo}: {c.antes} -> {c.despues}")
        if self.no_encontrados:
            lineas.append("  No encontrados: " + ", ".join(self.no_encontrados))
        for e in self.errores:
            lineas.append(f"  ERROR: {e}")
        return "\n".join(lineas)

    def a_dict(self) -> dict:
        return {
            "ejecucion": self.ejecucion,
            "simulacro": self.simulacro,
            "cambios": [c.__dict__ for c in self.cambios],
            "items_actualizados": self.items_actualizados,
            "sin_cambios": self.sin_cambios,
            "no_encontrados": self.no_encontrados,
            "errores": self.errores,
        }


def _distinto_precio(a, b) -> bool:
    return a is None or abs(float(a) - float(b)) >= 0.005


def sincronizar(config: Config, ml: ClienteML, filas: list[FilaFuente], simulacro: bool | None = None) -> Reporte:
    simulacro = config.sync_simulacro if simulacro is None else simulacro
    rep = Reporte(ejecucion=uuid.uuid4().hex[:10], simulacro=simulacro)
    fuente = {f.sku: f for f in filas}

    user_id = ml.tokens.user_id() or ml.yo()["id"]
    ids = ml.buscar_items_vendedor(user_id)
    items = [i for i in ml.items(ids) if i.get("status") in ESTADOS_SINCRONIZABLES]
    log.info("Publicaciones del vendedor: %d (sincronizables: %d)", len(ids), len(items))

    encontrados: set[str] = set()
    for item in items:
        variaciones = item.get("variations") or []
        if variaciones:
            _planificar_variaciones(config, item, variaciones, fuente, encontrados, rep, simulacro, ml)
        else:
            _planificar_simple(config, item, fuente, encontrados, rep, simulacro, ml)

    rep.no_encontrados = sorted(set(fuente) - encontrados)
    _registrar(config, rep)
    log.info(rep.resumen())
    return rep


def _planificar_simple(config, item, fuente, encontrados, rep, simulacro, ml):
    sku = sku_de(item)
    if not sku or sku not in fuente:
        return
    encontrados.add(sku)
    f = fuente[sku]
    cuerpo, cambios = {}, []
    if config.sync_actualizar_stock and f.stock is not None and f.stock != item.get("available_quantity"):
        cuerpo["available_quantity"] = f.stock
        cambios.append(Cambio(sku, item["id"], None, "stock", item.get("available_quantity"), f.stock))
    if config.sync_actualizar_precio and f.precio is not None and _distinto_precio(item.get("price"), f.precio):
        cuerpo["price"] = f.precio
        cambios.append(Cambio(sku, item["id"], None, "precio", item.get("price"), f.precio))
    _aplicar(item["id"], cuerpo, cambios, rep, simulacro, ml)


def _planificar_variaciones(config, item, variaciones, fuente, encontrados, rep, simulacro, ml):
    lista, cambios, precios_nuevos = [], [], set()
    for v in variaciones:
        entrada = {"id": v["id"]}
        sku = sku_de(v)
        if sku and sku in fuente:
            encontrados.add(sku)
            f = fuente[sku]
            if config.sync_actualizar_stock and f.stock is not None and f.stock != v.get("available_quantity"):
                entrada["available_quantity"] = f.stock
                cambios.append(Cambio(sku, item["id"], v["id"], "stock", v.get("available_quantity"), f.stock))
            if config.sync_actualizar_precio and f.precio is not None:
                precios_nuevos.add(f.precio)
                if _distinto_precio(v.get("price"), f.precio):
                    entrada["price"] = f.precio
                    cambios.append(Cambio(sku, item["id"], v["id"], "precio", v.get("price"), f.precio))
        lista.append(entrada)
    if len(precios_nuevos) > 1:
        rep.errores.append(f"{item['id']}: la fuente tiene precios distintos para variaciones de la misma "
                           f"publicación ({sorted(precios_nuevos)}); Mercado Libre usa un único precio. No se tocó.")
        return
    if precios_nuevos and any("price" in e for e in lista):
        # Todas las variaciones deben quedar con el mismo precio.
        precio = precios_nuevos.pop()
        for e in lista:
            e["price"] = precio
    _aplicar(item["id"], {"variations": lista} if cambios else {}, cambios, rep, simulacro, ml)


def _aplicar(item_id, cuerpo, cambios, rep: Reporte, simulacro, ml: ClienteML):
    if not cambios:
        rep.sin_cambios += 1
        return
    if simulacro:
        rep.cambios.extend(cambios)
        return
    try:
        ml.actualizar_item(item_id, cuerpo)
        rep.cambios.extend(cambios)
        rep.items_actualizados.append(item_id)
        log.info("Actualizado %s: %s", item_id, cuerpo)
    except ErrorML as e:
        rep.errores.append(f"{item_id}: {e}")
        log.error("No se pudo actualizar %s: %s", item_id, e)


def _registrar(config: Config, rep: Reporte) -> None:
    con = conectar(config.db_path)
    try:
        con.execute("BEGIN")
        for c in rep.cambios:
            con.execute(
                """INSERT INTO sync_registro(ejecucion, sku, item_id, variacion_id, campo, antes, despues,
                   simulacro, resultado) VALUES (?,?,?,?,?,?,?,?,?)""",
                (rep.ejecucion, c.sku, c.item_id, c.variacion_id, c.campo, str(c.antes), str(c.despues),
                 int(rep.simulacro), "simulado" if rep.simulacro else "ok"))
        for e in rep.errores:
            con.execute("""INSERT INTO sync_registro(ejecucion, simulacro, resultado) VALUES (?,?,?)""",
                        (rep.ejecucion, int(rep.simulacro), f"error: {e}"))
        con.execute("COMMIT")
    finally:
        con.close()
