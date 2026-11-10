"""Punto de entrada del servidor de ingesta.

Uso típico::

    .venv/bin/python -m server.run --simulate     # sin ESP32 (pruebas)
    .venv/bin/python -m server.run                # con el ESP32 por USB

Además de arrancar uvicorn, imprime la URL del dashboard al arrancar: en
campo el operador necesita ver de un vistazo a qué dirección apuntar.

Por qué comprobar el puerto antes de arrancar: uvicorn termina con una
excepción si el puerto está ocupado. Se comprueba con un bind de prueba y, si
8000 está tomado, se usa el 8001 (decisión documentada en la tesis).
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys

import uvicorn

from . import config
from .app import create_app

log = logging.getLogger("server.run")


def _port_available(host: str, port: int) -> bool:
    """True si se puede hacer bind en host:port ahora mismo."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _pick_port(host: str, preferred: int) -> int:
    """Elige el puerto HTTP: el pedido, o el alternativo si está ocupado."""
    if _port_available(host, preferred):
        return preferred
    fallback = config.HTTP_FALLBACK_PORT
    if fallback != preferred and _port_available(host, fallback):
        log.warning(
            "Puerto %d ocupado; se usará el %d. Revisa qué proceso lo tiene: lsof -i :%d",
            preferred,
            fallback,
            preferred,
        )
        return fallback
    # Ninguno libre: se intenta el preferido y que uvicorn reporte el error,
    # que es más informativo que un fallo silencioso.
    return preferred


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="server.run",
        description="Servidor de ingesta del sistema de monitoreo estructural.",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="genera datos sintéticos (MPU6050 + ADXL345) sin abrir el puerto serie",
    )
    parser.add_argument(
        "--serial-port",
        default=config.SERIAL_PORT,
        help=f"puerto serie del ESP32 (por defecto: {config.SERIAL_PORT})",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=config.HTTP_PORT,
        help=f"puerto HTTP (por defecto: {config.HTTP_PORT})",
    )
    parser.add_argument(
        "--host",
        default=config.HTTP_HOST,
        help=f"dirección de escucha (por defecto: {config.HTTP_HOST})",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="ruta alternativa de la base de datos SQLite",
    )
    # --- política de retención (ver config.py y server/retention.py) -----
    # Cada bandera sobrescribe el valor por defecto de config.py, que a su vez
    # puede venir de la variable de entorno MONITOREO_*. Precedencia:
    # bandera > variable de entorno > valor por defecto.
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help=f"días de retención del detalle (por defecto: {config.RETENTION_DAYS})",
    )
    parser.add_argument(
        "--retention-max-rows",
        type=int,
        default=None,
        help=f"techo de filas de samples (por defecto: {config.RETENTION_MAX_ROWS})",
    )
    parser.add_argument(
        "--aggregate-after-days",
        type=int,
        default=None,
        help=(
            "días a partir de los cuales el detalle se agrega y se borra "
            f"(por defecto: {config.AGGREGATE_AFTER_DAYS})"
        ),
    )
    parser.add_argument(
        "--aggregate-bucket-min",
        type=int,
        default=None,
        help=(
            "tamaño del bucket de agregación en minutos "
            f"(por defecto: {config.AGGREGATE_BUCKET_MIN})"
        ),
    )
    parser.add_argument(
        "--retention-interval-s",
        type=float,
        default=None,
        help=(
            "segundos entre pasadas de retención "
            f"(por defecto: {config.RETENTION_INTERVAL_S})"
        ),
    )
    parser.add_argument(
        "--vacuum-interval-s",
        type=float,
        default=None,
        help=(
            "segundos entre intentos de vacuum; 0 lo desactiva "
            f"(por defecto: {config.RETENTION_VACUUM_INTERVAL_S})"
        ),
    )
    parser.add_argument(
        "--vacuum-full",
        action="store_true",
        default=None,
        help=(
            "permite VACUUM completo cuando incremental no está disponible "
            "(bloquea escrituras: usar en ventana de mantenimiento)"
        ),
    )
    args = parser.parse_args(argv)

    # Logging estándar en lugar de prints: los mensajes del hilo de ingesta
    # llegan por logging y deben verse igual que los del servidor HTTP.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    port = _pick_port(args.host, args.http_port)
    app = create_app(
        simulate=args.simulate,
        serial_port=args.serial_port,
        db_path=args.db,
        retention_days=args.retention_days,
        retention_max_rows=args.retention_max_rows,
        aggregate_after_days=args.aggregate_after_days,
        aggregate_bucket_min=args.aggregate_bucket_min,
        retention_interval_s=args.retention_interval_s,
        vacuum_interval_s=args.vacuum_interval_s,
        vacuum_full=args.vacuum_full,
    )

    # La URL se imprime al arrancar (requisito de operación en campo).
    url = f"http://{args.host}:{port}"
    log.info("=" * 60)
    log.info("Dashboard: %s", url)
    log.info("WebSocket: ws://%s:%d/ws", args.host, port)
    log.info("API:       %s/api/status", url)
    log.info("Fuente de datos: %s",
             "simulador (--simulate)" if args.simulate else args.serial_port)
    log.info(
        "Retención: %d días / %d filas máx / agregado > %d d en buckets de "
        "%d min / cada %s s (detalle: %s/api/db/stats)",
        args.retention_days if args.retention_days is not None else config.RETENTION_DAYS,
        args.retention_max_rows
        if args.retention_max_rows is not None
        else config.RETENTION_MAX_ROWS,
        args.aggregate_after_days
        if args.aggregate_after_days is not None
        else config.AGGREGATE_AFTER_DAYS,
        args.aggregate_bucket_min
        if args.aggregate_bucket_min is not None
        else config.AGGREGATE_BUCKET_MIN,
        args.retention_interval_s
        if args.retention_interval_s is not None
        else config.RETENTION_INTERVAL_S,
        url,
    )
    log.info("=" * 60)

    uvicorn.run(app, host=args.host, port=port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
