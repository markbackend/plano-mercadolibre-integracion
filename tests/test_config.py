import pytest

from app.config import RAIZ, cargar_config

VARIABLES = ["ML_CLIENT_ID", "ML_SITIO", "DB_PATH", "SYNC_SIMULACRO", "PREGUNTAS_MODO", "FUENTE_TIPO",
             "IA_ACTIVADA", "ANTHROPIC_MODELO"]


@pytest.fixture
def env_vacio(monkeypatch, tmp_path):
    for v in VARIABLES:
        monkeypatch.delenv(v, raising=False)
    archivo = tmp_path / "vacio.env"
    archivo.write_text("", encoding="utf-8")
    return str(archivo)


def test_valores_por_defecto(env_vacio):
    c = cargar_config(env_vacio)
    assert c.sitio == "MLA" and c.dominio_auth == "https://auth.mercadolibre.com.ar"
    assert c.sync_simulacro is True  # por defecto no toca nada
    assert c.preguntas_modo == "sugerir" and c.ia_activada is False
    assert c.anthropic_modelo == "claude-haiku-5-5"


def test_lee_variables_y_resuelve_rutas_relativas(env_vacio, monkeypatch):
    monkeypatch.setenv("ML_CLIENT_ID", "999")
    monkeypatch.setenv("ML_SITIO", "mlm")
    monkeypatch.setenv("DB_PATH", "datos/otra.db")
    monkeypatch.setenv("SYNC_SIMULACRO", "false")
    c = cargar_config(env_vacio)
    assert c.client_id == "999"
    assert c.dominio_auth == "https://auth.mercadolibre.com.mx"
    assert c.db_path == str(RAIZ / "datos" / "otra.db")
    assert c.sync_simulacro is False


@pytest.mark.parametrize("variable,valor", [("PREGUNTAS_MODO", "magia"), ("FUENTE_TIPO", "pdf")])
def test_valores_invalidos(env_vacio, monkeypatch, variable, valor):
    monkeypatch.setenv(variable, valor)
    with pytest.raises(ValueError):
        cargar_config(env_vacio)


def test_sitio_desconocido(env_vacio, monkeypatch):
    monkeypatch.setenv("ML_SITIO", "XXX")
    with pytest.raises(ValueError):
        cargar_config(env_vacio).dominio_auth
