from app.db import conectar
from app.fuente import FilaFuente, leer_fuente
from app.sincronizacion import sincronizar, sku_de


def F(sku, stock=None, precio=None):
    return FilaFuente(sku, stock, precio)


def test_sku_de_campo_y_atributo():
    assert sku_de({"seller_custom_field": " A1 "}) == "A1"
    assert sku_de({"attributes": [{"id": "SELLER_SKU", "value_name": "B2"}]}) == "B2"
    assert sku_de({"attributes": [{"id": "BRAND", "value_name": "X"}]}) is None


def test_simulacro_no_toca_nada(conectado, sim, config):
    rep = sincronizar(config, conectado.ml, leer_fuente(config), simulacro=True)
    assert sim.puts == []
    assert len(rep.cambios) == 9
    assert rep.no_encontrados == ["TERMO-1L"]
    assert rep.sin_cambios == 1  # CARG-20W ya está igual
    assert sim.items["MLA1000000001"]["available_quantity"] == 10


def test_simulacro_es_el_valor_por_defecto(conectado, sim, config):
    assert config.sync_simulacro is True
    sincronizar(config, conectado.ml, [F("AUR-X200", 1)])
    assert sim.puts == []


def test_aplica_stock_y_precio_en_todas_las_publicaciones_del_sku(conectado, sim, config):
    rep = sincronizar(config, conectado.ml, [F("AUR-X200", 3, 30000)], simulacro=False)
    assert sorted(rep.items_actualizados) == ["MLA1000000001", "MLA1000000005"]
    for i in ("MLA1000000001", "MLA1000000005"):
        assert sim.items[i]["available_quantity"] == 3
        assert sim.items[i]["price"] == 30000
    assert rep.errores == []


def test_variaciones_se_mandan_todas(conectado, sim, config):
    """Si faltara alguna variación en el PUT, Mercado Libre (y el simulador) la borraría."""
    sincronizar(config, conectado.ml, [F("REM-BLA-M", 7)], simulacro=False)
    item_id, cuerpo = sim.puts[0]
    assert item_id == "MLA1000000002"
    assert [v["id"] for v in cuerpo["variations"]] == [9001, 9002, 9003]
    assert cuerpo["variations"][2] == {"id": 9003, "available_quantity": 7}
    assert cuerpo["variations"][0] == {"id": 9001}
    vars_ = sim.items["MLA1000000002"]["variations"]
    assert len(vars_) == 3
    assert [v["available_quantity"] for v in vars_] == [5, 3, 7]


def test_precio_de_variaciones_unico(conectado, sim, config):
    sincronizar(config, conectado.ml, [F("REM-NEG-M", None, 9100)], simulacro=False)
    assert {v["price"] for v in sim.items["MLA1000000002"]["variations"]} == {9100.0}


def test_precios_distintos_en_variaciones_es_error(conectado, sim, config):
    rep = sincronizar(config, conectado.ml, [F("REM-NEG-M", 1, 9000), F("REM-NEG-L", 1, 9500)], simulacro=False)
    assert sim.puts == []
    assert "precios distintos" in rep.errores[0]


def test_publicaciones_cerradas_no_se_tocan(conectado, sim, config):
    rep = sincronizar(config, conectado.ml, [F("MATE-01", 5)], simulacro=False)
    assert sim.puts == []
    assert rep.no_encontrados == ["MATE-01"]


def test_solo_stock_si_se_desactiva_precio(conectado, sim, config):
    config.sync_actualizar_precio = False
    sincronizar(config, conectado.ml, [F("CARG-20W", 9, 1)], simulacro=False)
    assert sim.puts == [("MLA1000000003", {"available_quantity": 9})]


def test_error_de_la_api_se_informa_y_sigue(conectado, sim, config):
    sim.items["MLA1000000003"]["status"] = "paused"
    original = sim._put_item

    def put_que_falla(it, cambios):
        if it["id"] == "MLA1000000003":
            import httpx
            return httpx.Response(400, json={"error": "item.price.invalid"})
        return original(it, cambios)

    sim._put_item = put_que_falla
    rep = sincronizar(config, conectado.ml, [F("CARG-20W", 9), F("AUR-X200", 4)], simulacro=False)
    assert len(rep.errores) == 1 and "MLA1000000003" in rep.errores[0]
    assert sim.items["MLA1000000001"]["available_quantity"] == 4


def test_queda_registro_en_la_base(conectado, config):
    rep = sincronizar(config, conectado.ml, [F("AUR-X200", 2)], simulacro=True)
    con = conectar(config.db_path)
    filas = con.execute("SELECT * FROM sync_registro WHERE ejecucion=?", (rep.ejecucion,)).fetchall()
    con.close()
    assert len(filas) == 2 and all(f["simulacro"] == 1 for f in filas)
