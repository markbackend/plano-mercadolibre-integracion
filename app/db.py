"""Base de datos SQLite: tokens, notificaciones, preguntas, órdenes y registro de sincronización."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ESQUEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    user_id INTEGER,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expira_en REAL NOT NULL,
    actualizado TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS oauth_estados (
    state TEXT PRIMARY KEY,
    creado TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notificaciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clave TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL,
    resource TEXT NOT NULL,
    user_id INTEGER,
    cuerpo TEXT,
    estado TEXT NOT NULL DEFAULT 'pendiente',  -- pendiente | ok | error | ignorada
    intentos INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    recibido TEXT NOT NULL DEFAULT (datetime('now')),
    tomado TEXT,                          -- cuándo un worker la empezó a procesar
    procesado TEXT
);

CREATE TABLE IF NOT EXISTS preguntas (
    id INTEGER PRIMARY KEY,               -- id de la pregunta en Mercado Libre
    item_id TEXT,
    texto TEXT,
    estado TEXT NOT NULL,                 -- sugerida | respondida | sin_respuesta | descartada
    respuesta TEXT,
    origen TEXT,                          -- regla | ia
    regla TEXT,
    creado TEXT NOT NULL DEFAULT (datetime('now')),
    actualizado TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ordenes (
    id INTEGER PRIMARY KEY,               -- id de la orden en Mercado Libre
    estado TEXT,
    fecha TEXT,
    total REAL,
    moneda TEXT,
    comprador TEXT,
    items TEXT,                           -- JSON con los ítems
    avisada INTEGER NOT NULL DEFAULT 0,
    creado TEXT NOT NULL DEFAULT (datetime('now')),
    actualizado TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS items_cache (
    id TEXT PRIMARY KEY,                  -- id de la publicación (MLA...)
    titulo TEXT,
    precio REAL,
    stock INTEGER,
    estado TEXT,
    actualizado TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_registro (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ejecucion TEXT NOT NULL,
    sku TEXT,
    item_id TEXT,
    variacion_id INTEGER,
    campo TEXT,
    antes TEXT,
    despues TEXT,
    simulacro INTEGER NOT NULL,
    resultado TEXT,
    fecha TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def conectar(ruta: str) -> sqlite3.Connection:
    """Abre la base (la crea si no existe). isolation_level=None: las transacciones se manejan a mano."""
    if ruta != ":memory:":
        Path(ruta).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta, timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def inicializar(ruta: str) -> None:
    con = conectar(ruta)
    try:
        con.executescript(ESQUEMA)
    finally:
        con.close()


@contextmanager
def abrir(ruta: str) -> Iterator[sqlite3.Connection]:
    con = conectar(ruta)
    try:
        yield con
    finally:
        con.close()
