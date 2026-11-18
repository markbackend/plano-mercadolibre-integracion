"""Comandos de consola (para tareas programadas o uso manual).

    python -m app.cli autorizar              muestra el link para conectar la cuenta
    python -m app.cli code TG-xxxx [state]   cambia el code por tokens (si no usás el servidor)
    python -m app.cli sync [--aplicar]       sincroniza stock/precio (por defecto simulacro)
    python -m app.cli procesar               procesa la cola de notificaciones
    python -m app.cli preguntas              trae preguntas sin responder y sugiere respuestas
    python -m app.cli exportar [--csv]       exporta las órdenes a Excel (o CSV)
"""
from __future__ import annotations

import argparse
import json
import sys

from . import notificaciones, ordenes
from .config import cargar_config
from .fuente import ErrorFuente, leer_fuente
from .oauth import ErrorAutorizacion
from .cliente_ml import ErrorML
from .servicios import configurar_logs, crear_servicios
from .sincronizacion import sincronizar


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="app.cli", description="Integración con Mercado Libre")
    sub = p.add_subparsers(dest="comando", required=True)
    sub.add_parser("autorizar")
    c = sub.add_parser("code")
    c.add_argument("code")
    c.add_argument("state", nargs="?")
    s = sub.add_parser("sync")
    s.add_argument("--aplicar", action="store_true", help="hace los cambios de verdad (sin esto es simulacro)")
    sub.add_parser("procesar")
    sub.add_parser("preguntas")
    e = sub.add_parser("exportar")
    e.add_argument("--csv", action="store_true")
    args = p.parse_args(argv)

    configurar_logs()
    srv = crear_servicios(cargar_config())
    try:
        if args.comando == "autorizar":
            url, _ = srv.tokens.url_autorizacion()
            print("Abrí este link con la cuenta del vendedor:\n" + url)
        elif args.comando == "code":
            datos = srv.tokens.intercambiar_code(args.code, args.state)
            print(f"Cuenta {datos.get('user_id')} conectada.")
        elif args.comando == "sync":
            rep = sincronizar(srv.config, srv.ml, leer_fuente(srv.config, srv.http), simulacro=not args.aplicar)
            print(rep.resumen())
            return 1 if rep.errores else 0
        elif args.comando == "procesar":
            print(json.dumps(notificaciones.procesar_pendientes(srv), ensure_ascii=False))
        elif args.comando == "preguntas":
            uid = srv.tokens.user_id() or srv.ml.yo()["id"]
            for q in srv.ml.preguntas_sin_responder(uid):
                print(json.dumps(srv.preguntas.procesar(q["id"]), ensure_ascii=False))
        elif args.comando == "exportar":
            print(ordenes.exportar(srv.config, "csv" if args.csv else "xlsx"))
    except (ErrorAutorizacion, ErrorFuente, ErrorML) as ex:
        print(f"ERROR: {ex}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
