"""Lectura del stock y precio del cliente: CSV, Excel o una API propia.

Formato esperado (los nombres de columna aceptan variantes comunes):
    sku, stock, precio
Una celda vacía en stock o precio significa "no tocar ese dato".
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Config

log = logging.getLogger("ml.fuente")

ALIAS = {
    "sku": {"sku", "codigo", "código", "seller_sku", "cod", "articulo", "artículo"},
    "stock": {"stock", "cantidad", "available_quantity", "existencia", "disponible"},
    "precio": {"precio", "price", "precio_venta", "pvp"},
}


class ErrorFuente(Exception):
    pass


@dataclass
class FilaFuente:
    sku: str
    stock: int | None
    precio: float | None
    linea: int | None = None


def _normalizar_encabezado(nombre: str) -> str | None:
    n = str(nombre or "").strip().lower()
    for campo, variantes in ALIAS.items():
        if n in variantes:
            return campo
    return None


def _numero(valor, entero: bool, sku: str, linea):
    if valor is None or str(valor).strip() == "":
        return None
    texto = str(valor).strip().replace("$", "").replace(" ", "")
    # Acepta "1.234,50" (formato argentino) y "1234.50".
    if "," in texto and "." in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif "," in texto:
        texto = texto.replace(",", ".")
    try:
        n = float(texto)
    except ValueError:
        raise ErrorFuente(f"Línea {linea} (SKU {sku}): '{valor}' no es un número")
    if n < 0:
        raise ErrorFuente(f"Línea {linea} (SKU {sku}): valor negativo {n}")
    if entero:
        if n != int(n):
            raise ErrorFuente(f"Línea {linea} (SKU {sku}): el stock debe ser entero ({valor})")
        return int(n)
    return round(n, 2)


def _desde_filas(encabezados: list, filas: list[list], primera_linea: int = 2) -> list[FilaFuente]:
    mapa = {}
    for i, h in enumerate(encabezados):
        campo = _normalizar_encabezado(h)
        if campo and campo not in mapa:
            mapa[campo] = i
    if "sku" not in mapa:
        raise ErrorFuente(f"Falta la columna de SKU. Encabezados leídos: {encabezados}")
    if "stock" not in mapa and "precio" not in mapa:
        raise ErrorFuente("Tiene que haber al menos una columna de stock o de precio")
    salida: list[FilaFuente] = []
    vistos: set[str] = set()
    for n, fila in enumerate(filas, start=primera_linea):
        def celda(campo):
            i = mapa.get(campo)
            return fila[i] if i is not None and i < len(fila) else None
        sku = str(celda("sku") or "").strip()
        if not sku:
            continue  # filas vacías
        if sku in vistos:
            raise ErrorFuente(f"Línea {n}: el SKU {sku} está repetido en la fuente")
        vistos.add(sku)
        salida.append(FilaFuente(sku=sku, stock=_numero(celda("stock"), True, sku, n),
                                 precio=_numero(celda("precio"), False, sku, n), linea=n))
    return salida


def leer_csv(ruta: str) -> list[FilaFuente]:
    p = Path(ruta)
    if not p.exists():
        raise ErrorFuente(f"No existe el archivo {ruta}")
    texto = p.read_text(encoding="utf-8-sig")
    # Detecta separador ; (Excel en español) o ,
    if not texto.strip():
        raise ErrorFuente("El CSV está vacío")
    try:
        dialecto = csv.Sniffer().sniff(texto.splitlines()[0], delimiters=",;\t")
    except csv.Error:
        dialecto = csv.excel
    filas = list(csv.reader(texto.splitlines(), dialecto))
    if not filas:
        raise ErrorFuente("El CSV está vacío")
    return _desde_filas(filas[0], filas[1:])


def leer_excel(ruta: str) -> list[FilaFuente]:
    from openpyxl import load_workbook

    p = Path(ruta)
    if not p.exists():
        raise ErrorFuente(f"No existe el archivo {ruta}")
    libro = load_workbook(p, read_only=True, data_only=True)
    try:
        hoja = libro.worksheets[0]
        filas = [list(f) for f in hoja.iter_rows(values_only=True)]
    finally:
        libro.close()
    if not filas:
        raise ErrorFuente("El Excel está vacío")
    return _desde_filas(filas[0], filas[1:])


def leer_api(config: Config, http: httpx.Client) -> list[FilaFuente]:
    """API del cliente: GET FUENTE_API_URL -> [{"sku":..,"stock":..,"precio":..}] o {"productos": [...]}."""
    if not config.fuente_api_url:
        raise ErrorFuente("Falta FUENTE_API_URL")
    headers = {"Accept": "application/json"}
    if config.fuente_api_token:
        headers["Authorization"] = f"Bearer {config.fuente_api_token}"
    try:
        r = http.get(config.fuente_api_url, headers=headers, timeout=30)
        r.raise_for_status()
        datos = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ErrorFuente(f"No se pudo leer la API del cliente: {e}") from e
    if isinstance(datos, dict):
        datos = datos.get("productos") or datos.get("items") or datos.get("data") or []
    if not isinstance(datos, list):
        raise ErrorFuente("La API del cliente debe devolver una lista de productos")
    encabezados = ["sku", "stock", "precio"]
    filas = []
    for d in datos:
        fila = {(_normalizar_encabezado(k) or k): v for k, v in d.items()}
        filas.append([fila.get("sku"), fila.get("stock"), fila.get("precio")])
    return _desde_filas(encabezados, filas, primera_linea=1)


def leer_fuente(config: Config, http: httpx.Client | None = None) -> list[FilaFuente]:
    if config.fuente_tipo == "csv":
        filas = leer_csv(config.fuente_ruta)
    elif config.fuente_tipo == "excel":
        filas = leer_excel(config.fuente_ruta)
    else:
        filas = leer_api(config, http or httpx.Client())
    log.info("Fuente %s: %d productos leídos", config.fuente_tipo, len(filas))
    return filas
