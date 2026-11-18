import httpx
import pytest
from openpyxl import Workbook

from app.fuente import ErrorFuente, leer_api, leer_csv, leer_excel, leer_fuente


def escribir(tmp_path, texto, nombre="f.csv"):
    p = tmp_path / nombre
    p.write_text(texto, encoding="utf-8")
    return str(p)


def test_csv_ejemplo(config):
    filas = leer_fuente(config)
    assert [f.sku for f in filas][:2] == ["AUR-X200", "REM-NEG-M"]
    assert filas[0].stock == 8 and filas[0].precio == 27999


def test_csv_con_coma_y_alias(tmp_path):
    filas = leer_csv(escribir(tmp_path, "Codigo,Cantidad,Precio_Venta\nA1,3,10.5\nA2,,\n,,\n"))
    assert (filas[0].sku, filas[0].stock, filas[0].precio) == ("A1", 3, 10.5)
    assert (filas[1].stock, filas[1].precio) == (None, None)  # vacío = no tocar
    assert len(filas) == 2


def test_formato_argentino(tmp_path):
    filas = leer_csv(escribir(tmp_path, 'sku;precio\nA1;"$ 1.234,50"\nA2;99,9\n'))
    assert filas[0].precio == 1234.5
    assert filas[1].precio == 99.9


@pytest.mark.parametrize("texto,mensaje", [
    ("producto;stock\nA;1\n", "SKU"),
    ("sku;color\nA;rojo\n", "stock o de precio"),
    ("sku;stock\nA;1\nA;2\n", "repetido"),
    ("sku;stock\nA;-1\n", "negativo"),
    ("sku;stock\nA;1.5\n", "entero"),
    ("sku;stock\nA;muchos\n", "no es un número"),
    ("", "vacío"),
])
def test_errores_csv(tmp_path, texto, mensaje):
    with pytest.raises(ErrorFuente, match=mensaje):
        leer_csv(escribir(tmp_path, texto))


def test_archivo_inexistente(tmp_path):
    with pytest.raises(ErrorFuente, match="No existe"):
        leer_csv(str(tmp_path / "no.csv"))


def test_excel(tmp_path):
    libro = Workbook()
    h = libro.active
    h.append(["SKU", "Stock", "Precio"])
    h.append(["B1", 4, 1500])
    h.append([None, None, None])
    h.append(["B2", 0, 99.99])
    ruta = tmp_path / "stock.xlsx"
    libro.save(ruta)
    filas = leer_excel(str(ruta))
    assert [(f.sku, f.stock, f.precio) for f in filas] == [("B1", 4, 1500.0), ("B2", 0, 99.99)]


def test_api_del_cliente(config):
    vistos = {}

    def manejar(req):
        vistos["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json={"productos": [{"codigo": "C1", "cantidad": 7, "price": 100}]})

    config.fuente_api_url = "https://erp.cliente.local/stock"
    config.fuente_api_token = "token-erp"
    filas = leer_api(config, httpx.Client(transport=httpx.MockTransport(manejar)))
    assert (filas[0].sku, filas[0].stock, filas[0].precio) == ("C1", 7, 100.0)
    assert vistos["auth"] == "Bearer token-erp"


def test_api_del_cliente_caida(config):
    config.fuente_api_url = "https://erp.cliente.local/stock"
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(ErrorFuente, match="API del cliente"):
        leer_api(config, http)
