"""Respuestas a preguntas de compradores: reglas por palabras clave y, opcional, IA.

Modos (PREGUNTAS_MODO):
- sugerir (por defecto): se guarda una respuesta sugerida y una persona la aprueba o edita.
- automatico: se responde sola en Mercado Libre.
- apagado: solo se guardan las preguntas.

Nunca se inventan datos: las plantillas usan solo datos de la publicación, y si falta alguno la
regla no se aplica. Si nada aplica, la pregunta queda "sin_respuesta" para una persona.
"""
from __future__ import annotations

import json
import logging
import re
import string
import unicodedata
from pathlib import Path

import httpx

from . import ia
from .cliente_ml import ClienteML, ErrorML
from .config import Config
from .db import conectar

log = logging.getLogger("ml.preguntas")


def normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFD", texto or "")
    sin_tildes = "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn")
    sin_tildes = re.sub(r"[^\w\s]", " ", sin_tildes.lower())
    return " " + re.sub(r"\s+", " ", sin_tildes).strip() + " "


def cargar_reglas(ruta: str) -> dict:
    p = Path(ruta)
    if not p.exists():
        raise FileNotFoundError(f"No existe el archivo de reglas {ruta}")
    datos = json.loads(p.read_text(encoding="utf-8"))
    for r in datos.get("reglas", []):
        if not r.get("nombre") or not r.get("palabras") or not r.get("plantilla"):
            raise ValueError(f"Regla incompleta en {ruta}: {r}")
    return datos


def _contiene(texto_norm: str, palabra: str) -> bool:
    return f" {normalizar(palabra).strip()} " in texto_norm


CONDICIONES = {"new": "nuevo", "used": "usado", "refurbished": "reacondicionado"}


def _datos_plantilla(item: dict) -> dict:
    precio = item.get("price")
    return {
        "titulo": item.get("title"),
        "precio": (f"{precio:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                   if isinstance(precio, (int, float)) else None),
        "moneda": {"ARS": "$", "USD": "US$"}.get(item.get("currency_id"), item.get("currency_id")),
        "stock": item.get("available_quantity"),
        "condicion": CONDICIONES.get(item.get("condition")),
    }


def responder_por_reglas(reglas: dict, pregunta: str, item: dict) -> tuple[str | None, str | None, bool]:
    """Devuelve (respuesta, nombre_regla, derivar_a_persona)."""
    texto = normalizar(pregunta)
    if any(_contiene(texto, p) for p in reglas.get("derivar_a_persona", [])):
        return None, None, True
    datos = _datos_plantilla(item)
    stock = item.get("available_quantity") or 0
    mejor, mejor_puntaje = None, 0
    for r in reglas.get("reglas", []):
        if r.get("requiere_stock") and stock <= 0:
            continue
        if r.get("requiere_sin_stock") and stock > 0:
            continue
        puntaje = sum(1 for p in r["palabras"] if _contiene(texto, p))
        if puntaje > mejor_puntaje:
            campos = [c[1] for c in string.Formatter().parse(r["plantilla"]) if c[1]]
            if any(datos.get(c) in (None, "") for c in campos):
                log.info("Regla %s descartada: falta un dato de la publicación", r["nombre"])
                continue
            mejor, mejor_puntaje = r, puntaje
    if not mejor:
        return None, None, False
    cuerpo = mejor["plantilla"].format(**datos)
    partes = [reglas.get("saludo", "").strip(), cuerpo, reglas.get("firma", "").strip()]
    return " ".join(p for p in partes if p), mejor["nombre"], False


class GestorPreguntas:
    def __init__(self, config: Config, ml: ClienteML, http_ia: httpx.Client | None = None):
        self.config = config
        self.ml = ml
        self.http_ia = http_ia or ml.http

    def procesar(self, pregunta_id: int) -> dict:
        """Procesa una pregunta (llamado desde la notificación 'questions'). Idempotente."""
        con = conectar(self.config.db_path)
        try:
            previa = con.execute("SELECT estado FROM preguntas WHERE id = ?", (pregunta_id,)).fetchone()
        finally:
            con.close()
        if previa and previa["estado"] in ("respondida", "descartada"):
            log.info("Pregunta %s ya %s; se ignora", pregunta_id, previa["estado"])
            return {"id": pregunta_id, "estado": previa["estado"], "nuevo": False}

        q = self.ml.pregunta(pregunta_id)
        if q.get("status") != "UNANSWERED":
            self._guardar(pregunta_id, q.get("item_id"), q.get("text"), "respondida",
                          (q.get("answer") or {}).get("text"), "externa", None)
            return {"id": pregunta_id, "estado": "respondida", "nuevo": False}

        texto = q.get("text", "")
        if self.config.preguntas_modo == "apagado":
            self._guardar(pregunta_id, q.get("item_id"), texto, "sin_respuesta", None, None, None)
            return {"id": pregunta_id, "estado": "sin_respuesta"}

        item = self.ml.item(q["item_id"])
        reglas = cargar_reglas(self.config.reglas_path)
        respuesta, regla, derivar = responder_por_reglas(reglas, texto, item)
        origen = "regla" if respuesta else None

        if respuesta is None and not derivar and self.config.ia_activada:
            politicas = [r["plantilla"] for r in reglas.get("reglas", [])
                         if "{" not in r["plantilla"]]  # políticas fijas (envío, factura...)
            try:
                respuesta = ia.sugerir_respuesta(self.config, self.http_ia, texto, item, politicas)
                origen = "ia" if respuesta else None
            except ia.ErrorIA as e:
                log.error("IA no disponible para la pregunta %s: %s", pregunta_id, e)

        if respuesta is None:
            motivo = "derivada a persona" if derivar else "sin regla aplicable"
            log.info("Pregunta %s queda para una persona (%s)", pregunta_id, motivo)
            self._guardar(pregunta_id, q["item_id"], texto, "sin_respuesta", None, None,
                          "derivar" if derivar else None)
            return {"id": pregunta_id, "estado": "sin_respuesta", "motivo": motivo}

        # Las respuestas de IA siempre pasan por una persona salvo que el modo sea automático.
        if self.config.preguntas_modo == "automatico":
            try:
                self.ml.responder(pregunta_id, respuesta)
            except ErrorML as e:
                log.error("No se pudo responder la pregunta %s: %s", pregunta_id, e)
                self._guardar(pregunta_id, q["item_id"], texto, "sugerida", respuesta, origen, regla)
                return {"id": pregunta_id, "estado": "sugerida", "error": str(e)}
            self._guardar(pregunta_id, q["item_id"], texto, "respondida", respuesta, origen, regla)
            log.info("Pregunta %s respondida automáticamente (%s)", pregunta_id, origen)
            return {"id": pregunta_id, "estado": "respondida", "respuesta": respuesta, "origen": origen}

        self._guardar(pregunta_id, q["item_id"], texto, "sugerida", respuesta, origen, regla)
        log.info("Pregunta %s: respuesta sugerida (%s), espera aprobación", pregunta_id, origen)
        return {"id": pregunta_id, "estado": "sugerida", "respuesta": respuesta, "origen": origen}

    def aprobar(self, pregunta_id: int, texto: str | None = None) -> dict:
        """Una persona aprueba (y opcionalmente edita) la respuesta y se envía a Mercado Libre."""
        con = conectar(self.config.db_path)
        try:
            fila = con.execute("SELECT * FROM preguntas WHERE id = ?", (pregunta_id,)).fetchone()
        finally:
            con.close()
        if fila is None:
            raise KeyError(f"No existe la pregunta {pregunta_id}")
        if fila["estado"] == "respondida":
            raise ValueError("La pregunta ya fue respondida")
        respuesta = (texto or fila["respuesta"] or "").strip()
        if not respuesta:
            raise ValueError("No hay texto para responder")
        self.ml.responder(pregunta_id, respuesta)
        self._guardar(pregunta_id, fila["item_id"], fila["texto"], "respondida", respuesta,
                      fila["origen"] if not texto else "persona", fila["regla"])
        return {"id": pregunta_id, "estado": "respondida", "respuesta": respuesta}

    def descartar(self, pregunta_id: int) -> None:
        con = conectar(self.config.db_path)
        try:
            con.execute("UPDATE preguntas SET estado='descartada', actualizado=datetime('now') WHERE id=?",
                        (pregunta_id,))
        finally:
            con.close()

    def listar(self, estado: str | None = None) -> list[dict]:
        con = conectar(self.config.db_path)
        try:
            if estado:
                filas = con.execute("SELECT * FROM preguntas WHERE estado=? ORDER BY creado", (estado,))
            else:
                filas = con.execute("SELECT * FROM preguntas ORDER BY creado")
            return [dict(f) for f in filas]
        finally:
            con.close()

    def _guardar(self, pid, item_id, texto, estado, respuesta, origen, regla):
        con = conectar(self.config.db_path)
        try:
            con.execute(
                """INSERT INTO preguntas(id, item_id, texto, estado, respuesta, origen, regla)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET estado=excluded.estado, respuesta=excluded.respuesta,
                     origen=excluded.origen, regla=excluded.regla, actualizado=datetime('now')""",
                (pid, item_id, texto, estado, respuesta, origen, regla))
        finally:
            con.close()
