# Integración con Mercado Libre para vendedores

Plantilla base (Python + FastAPI + SQLite) para conectar el negocio de un vendedor con la API REST oficial
de Mercado Libre, usando httpx directo (sin SDK).

## Qué resuelve

- **Conectar la cuenta del vendedor** con OAuth 2.0: link de autorización, cambio del `code` por tokens,
  guardado en SQLite y **renovación automática** del `access_token` antes de que venza. El `refresh_token`
  rota en cada uso, así que la renovación se hace con un bloqueo (`BEGIN IMMEDIATE` de SQLite + lock de
  hilos): si dos procesos quieren renovar a la vez, uno renueva y el otro usa el token nuevo.
- **Sincronizar stock y precio** desde la planilla del cliente (CSV o Excel) o desde su propia API hacia las
  publicaciones (`PUT /items/{id}`), buscando por SKU (`seller_custom_field` o atributo `SELLER_SKU`),
  incluidas las **variaciones**. Modo **simulacro** (por defecto) que muestra qué cambiaría sin tocar nada.
  Reintenta ante `429` y errores 5xx respetando `Retry-After` o con espera exponencial.
- **Notificaciones (webhook)** de `orders_v2`, `questions` e `items`: responde 200 al instante, encola en
  SQLite y procesa en segundo plano consultando el recurso a la API. Sin duplicados (mismo `_id` o mismo
  recurso pendiente), con reintentos y recuperación de notificaciones trabadas.
- **Preguntas**: respuestas por reglas (palabras clave -> plantilla en `config/reglas_preguntas.json`) y,
  opcional, con IA (Anthropic Claude por httpx, modelo configurable, por defecto `claude-haiku-5-5`). Modo
  **sugerir** (una persona aprueba) o automático. Nunca inventa datos: si falta información la pregunta
  queda para una persona, y los reclamos/devoluciones siempre se derivan.
- **Órdenes**: guarda las nuevas, avisa al dueño una sola vez cuando están pagadas (webhook propio y/o
  Telegram; si fallan todos los canales se reintenta) y exporta a Excel o CSV.
- **Simulador local** de Mercado Libre (OAuth, items, preguntas, órdenes, notificaciones y 429) para la demo
  y las pruebas: nada sale a internet.

## Pedidos reales de Workana que cubre

- "Necesito integrar el stock de mi sistema/Excel con Mercado Libre para no vender sin stock."
- "Quiero que las preguntas de Mercado Libre se respondan automáticamente (envíos, factura, stock)."
- "Sincronizar precios y stock de 500 publicaciones con variaciones desde mi lista de precios."
- "Que me llegue un aviso por Telegram cada vez que vendo y tener un Excel con las ventas."
- "Conectar mi ERP / tienda propia con la API de Mercado Libre (OAuth, webhooks, órdenes)."
- "Bot con IA que sugiera respuestas a las preguntas de compradores y yo las apruebe."

## Estructura

```
app/
  config.py          variables de entorno (.env)
  oauth.py           OAuth 2.0: link, code -> tokens, renovación con bloqueo
  cliente_ml.py      cliente REST (token, 401 -> renovar, 429/5xx -> reintentar)
  fuente.py          lectura de la fuente del cliente (CSV, Excel, API)
  sincronizacion.py  stock/precio por SKU, variaciones, simulacro
  notificaciones.py  cola del webhook, sin duplicados, reintentos
  preguntas.py       reglas + IA + aprobación
  ia.py              Anthropic Claude por httpx
  ordenes.py         guardar, avisar, exportar
  avisos.py          webhook propio y Telegram
  main.py            servidor FastAPI
  cli.py             comandos de consola (tareas programadas)
  simulador.py       Mercado Libre falso (transporte httpx)
  simulador_servidor.py  el mismo simulador como servidor HTTP
config/reglas_preguntas.json   reglas y plantillas editables
ejemplos/stock_cliente.csv     planilla de ejemplo
demo.py                        demo de 1 minuto
tests/                         pruebas (sin internet, sin claves)
```

## Instalar

Requiere Python 3.12.

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt     # Windows
# Linux/Mac: .venv/bin/python -m pip install -r requirements.txt
```

## Probar en 1 minuto (sin cuentas ni claves)

```bash
.venv/Scripts/python.exe demo.py
```

Recorre todo contra el simulador: OAuth, sincronización en simulacro y real (con variaciones, renovación
del token y dos 429), webhook con 5 preguntas y una venta (incluido un reenvío duplicado), respuestas por
reglas e IA falsa, aprobación de una persona, aviso de la venta y export a Excel/CSV en `salidas/demo/`.

### Demo con servidores (para probar a mano con el navegador)

Dos terminales, desde la carpeta del proyecto:

```bash
# 1) Simulador de Mercado Libre en 127.0.0.1:8001
.venv/Scripts/python.exe -m app.simulador_servidor

# 2) La app en 127.0.0.1:8000 apuntando al simulador (cmd; en PowerShell: $env:ML_CLIENT_ID="...")
set ML_CLIENT_ID=1234567890123456
set ML_CLIENT_SECRET=secreto-demo
set ML_API_URL=http://127.0.0.1:8001
set ML_AUTH_URL=http://127.0.0.1:8001
set DB_PATH=salidas/manual.db
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

Abrir `http://127.0.0.1:8000/auth/inicio` (conecta la cuenta demo), después `http://127.0.0.1:8000/docs`.
Para disparar eventos: `POST http://127.0.0.1:8001/_sim/pregunta` con `{"item_id": "MLA1000000001",
"texto": "¿Hacen envíos?"}` o `POST /_sim/orden` con `{"item_id": "MLA1000000003"}`.

## Pruebas

```bash
.venv/Scripts/python.exe -m pytest
```

92 pruebas. Usan un transporte httpx falso (el simulador) y una base SQLite temporal por prueba: corren sin
internet y sin claves. Cubren OAuth (state, rotación del refresh_token, renovación concurrente con un solo
refresh, 401, 429), sincronización (SKU por campo/atributo, variaciones, simulacro, 429, errores de la
API), fuente (CSV con `;` o `,`, formato argentino de números, Excel, API), webhook (duplicados, reintentos,
trabadas), preguntas (reglas, derivación, IA, modos), órdenes (avisos, export) y configuración.

## Configurar para un cliente real

1. Copiar `.env.example` a `.env` y completar.
2. **Crear la aplicación en Mercado Libre** (verificar estos pasos en la documentación vigente de
   developers.mercadolibre.com.ar, porque el portal cambia):
   1. Entrar al portal de desarrolladores con la cuenta **del vendedor** (o una cuenta del cliente) y crear
      una aplicación nueva ("Crear aplicación").
   2. Cargar nombre, descripción y logo.
   3. **URI de redirect**: la URL pública de `/auth/callback` (en producción tiene que ser https). Debe
      coincidir exacto con `ML_REDIRECT_URI`.
   4. Scopes / permisos: lectura, escritura y `offline_access` (este último es el que da refresh_token).
   5. **Tópicos de notificaciones**: marcar `orders_v2`, `questions` e `items`, y cargar la URL de
      notificaciones: `https://su-dominio/notificaciones`.
   6. Guardar y copiar el **App ID** (`ML_CLIENT_ID`) y la **Secret Key** (`ML_CLIENT_SECRET`).
3. Levantar el servidor, entrar a `/auth/inicio` con la cuenta del vendedor y aceptar. El vendedor lo hace
   él mismo: nunca pedirle la contraseña.
4. Correr `python -m app.cli sync` (simulacro), revisar el resumen con el cliente y recién después
   `python -m app.cli sync --aplicar` o poner `SYNC_SIMULACRO=false`.

### Comandos de consola

```bash
python -m app.cli autorizar          # link para conectar la cuenta
python -m app.cli sync [--aplicar]   # sincroniza (por defecto simulacro)
python -m app.cli procesar           # procesa la cola de notificaciones
python -m app.cli preguntas          # trae preguntas sin responder y sugiere respuestas
python -m app.cli exportar [--csv]   # exporta órdenes
```

### Endpoints

| Método | Ruta | Para qué |
|---|---|---|
| GET | `/salud` | estado y cola |
| GET | `/auth/inicio`, `/auth/callback` | OAuth |
| POST | `/notificaciones` | webhook de Mercado Libre |
| POST | `/admin/sync?simulacro=true` | sincronizar |
| GET | `/admin/preguntas?estado=sugerida` | preguntas |
| POST | `/admin/preguntas/{id}/aprobar` | enviar respuesta (opcional `{"texto": "..."}` para editarla) |
| POST | `/admin/preguntas/{id}/descartar` | descartar |
| GET | `/admin/ordenes`, `/admin/ordenes/exportar?formato=xlsx` | órdenes |
| GET/POST | `/admin/notificaciones`, `/admin/notificaciones/procesar` | cola |

Los `/admin` exigen el encabezado `X-Admin-Token` si `ADMIN_TOKEN` está configurado (obligatorio en producción).

## Desplegar

- Cualquier VPS o PaaS con Python 3.12 (Railway, Render, un VPS con Nginx). Comando:
  `uvicorn app.main:app --host 0.0.0.0 --port 8000`. Necesita **https** y dominio público (Mercado Libre
  exige https para el redirect y las notificaciones).
- Disco persistente para `DB_PATH` (ahí están los tokens). Con SQLite usar **un solo servidor**; el bloqueo
  de renovación funciona entre procesos de la misma máquina.
- Tareas programadas (cron / Programador de tareas): `python -m app.cli sync --aplicar` cada 15-60 minutos y
  `python -m app.cli procesar` cada 5 minutos como respaldo de la cola.
- Logs: salen por consola con fecha, nivel y módulo (`ml.oauth`, `ml.sync`, ...). No se loguean tokens.

## Límites conocidos (revisar en cada cliente)

- La búsqueda de publicaciones llega hasta 1000; para más hay que usar `search_type=scan` en
  `ClienteML.buscar_items_vendedor`.
- Todas las variaciones de una publicación comparten precio: si la planilla da precios distintos, se informa
  como error y no se toca esa publicación.
- Un mismo SKU en varias publicaciones (por ejemplo clásica y con cuotas) recibe el mismo precio. Si el
  cliente quiere precios distintos por tipo de publicación, hay que agregar una regla (ver checklist).
- Publicaciones con catálogo, Full o promociones activas pueden rechazar cambios de precio/stock: se
  informan como error en el reporte.
- El simulador sigue la documentación pública, no es Mercado Libre real: probar con una cuenta de prueba
  (usuarios de test de la API) antes de aplicar cambios de verdad.

## CÓMO ADAPTARLO A UN CLIENTE

| # | Qué cambiar | Dónde | Tiempo |
|---|---|---|---|
| 1 | Crear la aplicación en Mercado Libre y completar `.env` | portal + `.env` | 30 min |
| 2 | Sitio (país) si no es Argentina | `ML_SITIO` en `.env` | 2 min |
| 3 | Columnas de la planilla del cliente (nombres raros) | `ALIAS` en `app/fuente.py` | 15 min |
| 4 | Si la fuente es su API: formato de respuesta y autenticación | `leer_api` en `app/fuente.py` | 30-90 min |
| 5 | Dónde está el SKU en sus publicaciones (campo, atributo u otro) | `sku_de` en `app/sincronizacion.py` | 15 min |
| 6 | Reglas especiales de precio (recargo por cuotas, redondeo, margen) | `_planificar_simple` / `_planificar_variaciones` | 30-60 min |
| 7 | Respuestas a preguntas: palabras, textos, saludo, firma y qué derivar | `config/reglas_preguntas.json` | 20-40 min |
| 8 | IA: activar, modelo, tono y políticas del vendedor | `.env` + `SISTEMA` en `app/ia.py` | 20 min |
| 9 | Aviso de ventas: Telegram o webhook y texto del mensaje | `.env` + `procesar_orden` en `app/ordenes.py` | 15 min |
| 10 | Columnas del Excel de órdenes | `COLUMNAS` y `_filas_export` en `app/ordenes.py` | 15 min |
| 11 | Más de 1000 publicaciones | `buscar_items_vendedor` en `app/cliente_ml.py` | 30 min |
| 12 | Despliegue con https, cron y prueba con cuenta de test | servidor | 60-90 min |

Total típico: **2 a 6 horas** según la fuente de datos y las reglas de precio.

## Qué preguntarle al cliente

1. ¿En qué país vende y cuántas publicaciones tiene (aproximado)? ¿Usa variaciones (talle, color)?
2. ¿Dónde está hoy su stock y su lista de precios: Excel, sistema propio, otra tienda? ¿Puede pasarme un
   ejemplo de esa planilla o de la respuesta de su API?
3. ¿Tiene cargado el SKU en las publicaciones de Mercado Libre? ¿Coincide con el de su sistema?
4. ¿El precio en Mercado Libre es igual al de su lista o lleva recargo (comisión, cuotas, redondeo)?
5. ¿Cada cuánto quiere que se sincronice y qué tiene que pasar si un producto no está en la planilla?
6. ¿Qué preguntas le hacen más seguido y cómo las responde hoy? ¿Quiere que se respondan solas o revisarlas
   antes?
7. ¿Cómo quiere enterarse de las ventas (Telegram, email, su sistema) y qué datos necesita en el Excel?
8. ¿Tiene servidor o dominio con https, o lo incluimos en el trabajo?

## Licencia

MIT, ver `LICENSE`.
