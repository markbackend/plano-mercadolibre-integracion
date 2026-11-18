import threading
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.oauth import ErrorAutorizacion, GestorTokens


def test_url_autorizacion_argentina_por_defecto(servicios, config):
    url, state = servicios.tokens.url_autorizacion()
    u = urlparse(url)
    assert u.netloc == "auth.mercadolibre.com.ar"
    assert u.path == "/authorization"
    q = parse_qs(u.query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == [config.client_id]
    assert q["redirect_uri"] == [config.redirect_uri]
    assert q["state"] == [state]


def test_url_autorizacion_otro_sitio(servicios, config):
    config.sitio = "MLB"
    url, _ = servicios.tokens.url_autorizacion()
    assert url.startswith("https://auth.mercadolivre.com.br/authorization?")
    config.sitio = "XXX"
    with pytest.raises(ValueError):
        servicios.tokens.url_autorizacion()


def test_intercambio_de_code_guarda_tokens(servicios, sim, config, reloj):
    _, state = servicios.tokens.url_autorizacion()
    datos = servicios.tokens.intercambiar_code(sim.emitir_code(), state)
    assert datos["user_id"] == 123456789
    assert servicios.tokens.user_id() == 123456789
    assert servicios.tokens.token_valido() == datos["access_token"]


def test_state_invalido_se_rechaza(servicios, sim):
    servicios.tokens.url_autorizacion()
    with pytest.raises(ErrorAutorizacion, match="state"):
        servicios.tokens.intercambiar_code(sim.emitir_code(), "state-falso")


def test_state_no_se_puede_reusar(servicios, sim):
    _, state = servicios.tokens.url_autorizacion()
    servicios.tokens.intercambiar_code(sim.emitir_code(), state)
    with pytest.raises(ErrorAutorizacion):
        servicios.tokens.intercambiar_code(sim.emitir_code(), state)


def test_code_invalido(servicios):
    _, state = servicios.tokens.url_autorizacion()
    with pytest.raises(ErrorAutorizacion, match="rechazó"):
        servicios.tokens.intercambiar_code("TG-inexistente", state)


def test_sin_tokens_pide_autorizar(servicios):
    with pytest.raises(ErrorAutorizacion, match="/auth/inicio"):
        servicios.tokens.token_valido()


def test_no_renueva_si_el_token_esta_vigente(conectado, sim, reloj):
    t1 = conectado.tokens.token_valido()
    reloj.t += 3600
    assert conectado.tokens.token_valido() == t1
    assert sim.refresh_usados == 0


def test_renueva_antes_de_vencer_y_el_refresh_rota(conectado, sim, reloj, config):
    t1 = conectado.tokens.token_valido()
    viejo_refresh = next(iter(sim.refresh_validos))
    reloj.t += 21600 - config.margen_renovacion_seg + 1  # entra en el margen de renovación
    t2 = conectado.tokens.token_valido()
    assert t2 != t1
    assert sim.refresh_usados == 1
    assert viejo_refresh not in sim.refresh_validos  # el refresh_token viejo ya no sirve
    assert conectado.tokens.token_valido() == t2  # el nuevo se guardó


def test_refresh_rechazado_pide_reautorizar(conectado, sim, reloj):
    sim.refresh_validos.clear()  # por ejemplo, el vendedor revocó el permiso
    reloj.t += 30000
    with pytest.raises(ErrorAutorizacion, match="volver a autorizar"):
        conectado.tokens.token_valido()


def test_renovacion_concurrente_usa_un_solo_refresh(conectado, sim, reloj, config, transporte):
    """Varios 'procesos' (gestores distintos con la misma base) renuevan a la vez: solo uno gasta
    el refresh_token; los demás leen el token nuevo. Si no hubiera bloqueo, el segundo fallaría
    con invalid_grant porque el refresh_token rota."""
    reloj.t += 30000
    gestores = [GestorTokens(config, httpx.Client(transport=transporte), reloj=reloj) for _ in range(6)]
    barrera = threading.Barrier(len(gestores))
    resultados, errores = [], []

    def trabajar(g):
        barrera.wait()
        try:
            resultados.append(g.token_valido())
        except Exception as e:  # noqa: BLE001
            errores.append(e)

    hilos = [threading.Thread(target=trabajar, args=(g,)) for g in gestores]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert errores == []
    assert sim.refresh_usados == 1
    assert len(set(resultados)) == 1


def test_401_renueva_y_reintenta(conectado, sim):
    sim.access.clear()  # Mercado Libre invalidó el token antes de tiempo
    assert conectado.ml.yo()["id"] == 123456789
    assert sim.refresh_usados == 1


def test_token_con_429_reintenta(conectado, sim, reloj, esperas):
    reloj.t += 30000
    sim.responder_429 = 1
    conectado.tokens.token_valido()
    assert esperas == [1.0]
    assert sim.refresh_usados == 1
