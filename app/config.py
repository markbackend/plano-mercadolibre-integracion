"""Configuración leída de variables de entorno (o del archivo .env).

Nunca se escriben claves en el código: todo sale de acá.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent

# Dominio de autorización por sitio. La API (api.mercadolibre.com) es la misma para todos.
# Verificar en la documentación vigente si se agrega un sitio nuevo.
DOMINIOS_AUTH = {
    "MLA": "https://auth.mercadolibre.com.ar",  # Argentina
    "MLB": "https://auth.mercadolivre.com.br",  # Brasil
    "MLM": "https://auth.mercadolibre.com.mx",  # México
    "MLC": "https://auth.mercadolibre.cl",  # Chile
    "MCO": "https://auth.mercadolibre.com.co",  # Colombia
    "MLU": "https://auth.mercadolibre.com.uy",  # Uruguay
    "MPE": "https://auth.mercadolibre.com.pe",  # Perú
}


def _bool(valor: str | None, defecto: bool = False) -> bool:
    if valor is None or valor.strip() == "":
        return defecto
    return valor.strip().lower() in ("1", "true", "si", "sí", "yes", "on")


@dataclass
class Config:
    # Aplicación de Mercado Libre
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://localhost:8000/auth/callback"
    sitio: str = "MLA"
    api_url: str = "https://api.mercadolibre.com"
    auth_url: str = ""  # si está vacío se deduce del sitio
    margen_renovacion_seg: int = 600  # renovar el token 10 minutos antes de que venza

    # Base de datos y archivos
    db_path: str = str(RAIZ / "datos" / "mercadolibre.db")
    reglas_path: str = str(RAIZ / "config" / "reglas_preguntas.json")
    salidas_dir: str = str(RAIZ / "salidas")

    # Sincronización de stock/precio
    fuente_tipo: str = "csv"  # csv | excel | api
    fuente_ruta: str = str(RAIZ / "ejemplos" / "stock_cliente.csv")
    fuente_api_url: str = ""
    fuente_api_token: str = ""
    sync_simulacro: bool = True  # por defecto NO toca nada
    sync_actualizar_precio: bool = True
    sync_actualizar_stock: bool = True

    # Reintentos ante 429 / errores 5xx
    max_reintentos: int = 5
    espera_base_seg: float = 1.0

    # Preguntas
    preguntas_modo: str = "sugerir"  # sugerir | automatico | apagado
    ia_activada: bool = False
    anthropic_api_key: str = ""
    anthropic_modelo: str = "claude-haiku-5-5"
    anthropic_url: str = "https://api.anthropic.com/v1/messages"

    # Avisos de órdenes
    aviso_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_api_url: str = "https://api.telegram.org"

    # Seguridad del panel propio (endpoints de administración)
    admin_token: str = ""

    extra: dict = field(default_factory=dict)

    @property
    def dominio_auth(self) -> str:
        if self.auth_url:
            return self.auth_url.rstrip("/")
        if self.sitio not in DOMINIOS_AUTH:
            raise ValueError(f"Sitio desconocido: {self.sitio}. Opciones: {', '.join(DOMINIOS_AUTH)}")
        return DOMINIOS_AUTH[self.sitio]


def cargar_config(archivo_env: str | None = None) -> Config:
    """Lee .env (si existe) y las variables de entorno."""
    load_dotenv(archivo_env or RAIZ / ".env", override=False)
    e = os.environ.get
    c = Config()
    c.client_id = e("ML_CLIENT_ID", c.client_id)
    c.client_secret = e("ML_CLIENT_SECRET", c.client_secret)
    c.redirect_uri = e("ML_REDIRECT_URI", c.redirect_uri)
    c.sitio = e("ML_SITIO", c.sitio).upper()
    c.api_url = e("ML_API_URL", c.api_url).rstrip("/")
    c.auth_url = e("ML_AUTH_URL", c.auth_url)
    c.margen_renovacion_seg = int(e("ML_MARGEN_RENOVACION_SEG", str(c.margen_renovacion_seg)))
    c.db_path = e("DB_PATH", c.db_path)
    c.reglas_path = e("REGLAS_PREGUNTAS_PATH", c.reglas_path)
    c.salidas_dir = e("SALIDAS_DIR", c.salidas_dir)
    c.fuente_tipo = e("FUENTE_TIPO", c.fuente_tipo).lower()
    c.fuente_ruta = e("FUENTE_RUTA", c.fuente_ruta)
    c.fuente_api_url = e("FUENTE_API_URL", c.fuente_api_url)
    c.fuente_api_token = e("FUENTE_API_TOKEN", c.fuente_api_token)
    c.sync_simulacro = _bool(e("SYNC_SIMULACRO"), True)
    c.sync_actualizar_precio = _bool(e("SYNC_ACTUALIZAR_PRECIO"), True)
    c.sync_actualizar_stock = _bool(e("SYNC_ACTUALIZAR_STOCK"), True)
    c.max_reintentos = int(e("ML_MAX_REINTENTOS", str(c.max_reintentos)))
    c.espera_base_seg = float(e("ML_ESPERA_BASE_SEG", str(c.espera_base_seg)))
    c.preguntas_modo = e("PREGUNTAS_MODO", c.preguntas_modo).lower()
    c.ia_activada = _bool(e("IA_ACTIVADA"), False)
    c.anthropic_api_key = e("ANTHROPIC_API_KEY", c.anthropic_api_key)
    c.anthropic_modelo = e("ANTHROPIC_MODELO", c.anthropic_modelo)
    c.anthropic_url = e("ANTHROPIC_URL", c.anthropic_url)
    c.aviso_webhook_url = e("AVISO_WEBHOOK_URL", c.aviso_webhook_url)
    c.telegram_bot_token = e("TELEGRAM_BOT_TOKEN", c.telegram_bot_token)
    c.telegram_chat_id = e("TELEGRAM_CHAT_ID", c.telegram_chat_id)
    c.admin_token = e("ADMIN_TOKEN", c.admin_token)
    # Rutas relativas del .env: se toman desde la carpeta del proyecto, no desde donde se lanza.
    for campo in ("db_path", "reglas_path", "salidas_dir", "fuente_ruta"):
        valor = getattr(c, campo)
        if valor and not Path(valor).is_absolute():
            setattr(c, campo, str(RAIZ / valor))
    if c.preguntas_modo not in ("sugerir", "automatico", "apagado"):
        raise ValueError("PREGUNTAS_MODO debe ser sugerir, automatico o apagado")
    if c.fuente_tipo not in ("csv", "excel", "api"):
        raise ValueError("FUENTE_TIPO debe ser csv, excel o api")
    return c
