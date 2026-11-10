"""Configuración central del servidor de ingesta.

Todos los nombres y constantes que aparecen en ``CONTRATO_DE_DATOS.md`` se
definen aquí una sola vez. Si el contrato cambia, solo cambia este archivo
(los demás módulos importan de aquí, no reescriben literales).

Por qué un módulo de configuración y no solo variables de entorno: el sistema
debe funcionar en campo sin configuración alguna ("cero configuración" según la
sección 3 del contrato). Los valores por defecto son los del despliegue real y
``run.py`` permite sobrescribirlos por línea de comandos cuando hace falta. La
única excepción es el bloque de retención: allí cada constante admite además
una variable de entorno ``MONITOREO_*`` porque la política de purga depende del
disco de cada estación base y hay que poder ajustarla sin editar código.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    """Lee una variable de entorno entera o devuelve el valor por defecto.

    Una variable presente pero mal escrita lanza ValueError: un silencio aquí
    sería una política de retención distinta a la que el operador cree que
    configuró, que es peor que no arrancar.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} debe ser un entero; vale {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    """Lee una variable de entorno booleana (1/true/yes/on = True)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

# ---------------------------------------------------------------------------
# Rutas del proyecto
# ---------------------------------------------------------------------------
# server/config.py -> server/ -> dashboard/
BASE_DIR = Path(__file__).resolve().parent.parent

# SQLite local: un solo archivo, portable y exportable (contrato §3).
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "monitoreo.db"

# Frontend estático (HTML/CSS/JS sin build). Se sirve en "/" desde app.py.
# Es un hermano de server/, no un subdirectorio: el frontend debe poder
# editarse sin tocar el código Python.
WEB_DIR = BASE_DIR / "web"

# ---------------------------------------------------------------------------
# Servidor HTTP
# ---------------------------------------------------------------------------
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8000
# Si el puerto está ocupado (otra instancia, otro servicio) usamos el
# siguiente libre: en campo hay que poder levantar el dashboard aunque
# alguien haya dejado algo escuchando en 8000.
HTTP_FALLBACK_PORT = 8001

# ---------------------------------------------------------------------------
# Puerto serie (ESP32 por USB)
# ---------------------------------------------------------------------------
SERIAL_PORT = "/dev/cu.usbserial-0001"
SERIAL_BAUD = 115200  # contrato §2: 115200 8N1
SERIAL_READ_TIMEOUT_S = 0.2  # lectura no bloqueante para poder parar limpio
RECONNECT_DELAY_S = 2.0  # reintento de apertura del puerto (como el visualizer)

# ---------------------------------------------------------------------------
# Umbrales temporales (contrato §2.3 y §6)
# ---------------------------------------------------------------------------
# Sin muestras válidas durante 2 s el contrato obliga a marcar connected=false.
STALE_AFTER_MS = 2000
# Ventana de vibración (contrato §5): 1 segundo.
VIB_WINDOW_MS = 1000
# Ventana de agregación de eventos idénticos (ver EventCollapser en ingest.py):
# un bus I2C degradado repite la misma línea de error ~10 veces por segundo;
# sin agregación la consola de eventos se inunda hasta ser inútil. Con esta
# ventana el primer aviso sale inmediatamente y las repeticiones se publican
# como un único "(×N repeticiones)" como máximo cada 30 s.
EVENT_DEDUP_S = 30.0

# ---------------------------------------------------------------------------
# Escritura en base de datos por lotes
# ---------------------------------------------------------------------------
# La ingesta a 50 Hz (2 sensores => 100 filas/s) no puede hacer un INSERT por
# muestra: se acumulan y se vuelcan al superar 50 filas o 200 ms, lo que
# primero ocurra. Así el hilo HTTP nunca espera a la tasa de muestreo.
BATCH_MAX_ROWS = 50
BATCH_FLUSH_MS = 200

# ---------------------------------------------------------------------------
# Retención y agregación (crecimiento de la base de datos)
# ---------------------------------------------------------------------------
# POR QUÉ existe este bloque: la tabla ``samples`` crece sin límite. Cifras
# medidas en el despliegue real (oct-2026): data/monitoreo.db = 32.968.704 B
# con 119.865 muestras ≈ 275 B/fila. A 50 Hz x 2 sensores:
#
#   100 muestras/s * 86.400 s/día  = 8.640.000 filas/día
#   8,64 M filas/día * 275 B/fila ≈ 2,4 GB/día  (~2,2 GB/día con 250 B/fila)
#   => ~65 GB/mes y ~780-870 GB/año en un solo archivo SQLite.
#
# Sin política de retención, el disco de la estación base de Río Santiago se
# llena en semanas. Los tres mecanismos que lo acotan (retention.py):
#
#   1. Agregación histórica: el detalle con más de AGGREGATE_AFTER_DAYS se
#      reduce a promedios/picos por bucket de AGGREGATE_BUCKET_MIN en
#      ``samples_agg`` y se borra de ``samples``.
#   2. Retención por edad: nada sobrevive a RETENTION_DAYS (red de seguridad
#      si la agregación falla o se configura fuera de orden).
#   3. Techo de filas: RETENTION_MAX_ROWS limita el archivo aunque la tasa de
#      entrada sea mayor que la prevista (a 100 filas/s el techo de 5 M se
#      alcanza en 5 M / 100 ≈ 50.000 s ≈ 13,9 h, y el detalle se agrega antes
#      de expulsarse: las tendencias nunca se pierden).
#
# Equilibrio resultante: detalle acotado a ~5 M filas ≈ 1,3 GB + agregados que
# crecen ~288 buckets/día x 2 sensores x 10 métricas = 5.760 filas/día ≈ 1,4
# MB/día ≈ 0,5 GB/año. Comparado con los ~780 GB/año sin agregar.

# Días de retención del detalle en ``samples``. Variable de entorno
# MONITOREO_RETENTION_DAYS; bandera --retention-days en run.py.
RETENTION_DAYS = _env_int("MONITOREO_RETENTION_DAYS", 30)

# Techo duro de filas de ``samples``. Es el que manda a la tasa de diseño
# (5 M filas ≈ 13,9 h a 100 filas/s): evita que un sensor más rápido o una
# fuga de datos desborden el disco aunque no se cumpla la edad.
RETENTION_MAX_ROWS = _env_int("MONITOREO_RETENTION_MAX_ROWS", 5_000_000)

# A partir de esta edad, el detalle se reduce a ``samples_agg``. Por debajo de
# RETENTION_DAYS para que la agregación ocurra siempre antes de cualquier
# borrado por edad: nada se elimina sin agregarse antes.
AGGREGATE_AFTER_DAYS = _env_int("MONITOREO_AGGREGATE_AFTER_DAYS", 7)

# Tamaño del bucket de agregación en minutos. 5 min = 288 buckets/día, una
# resolución suficiente para tendencias estructurales (modos de la torre del
# orden de segundos-minutos) y una reducción de ~6.000x por fila.
AGGREGATE_BUCKET_MIN = _env_int("MONITOREO_AGGREGATE_BUCKET_MIN", 5)
AGGREGATE_BUCKET_MS = AGGREGATE_BUCKET_MIN * 60_000

# Segundos entre pasadas de retención. Cada pasada cuesta poco cuando el
# trabajo pendiente es de una hora de datos (~360k filas agregadas de 12 en 12
# buckets por transacción), así que una hora es un intervalo conservador.
RETENTION_INTERVAL_S = _env_int("MONITOREO_RETENTION_INTERVAL_S", 3600)

# Buckets agregados por transacción (12 x 5 min = 1 h de datos). Acota el
# tiempo que la retención retiene el cerrojo de escritura: la ingesta a 50 Hz
# sigue escribiendo entre transacciones.
AGGREGATE_CHUNK_BUCKETS = 12

# Filas borradas por transacción en la purga por edad (misma razón: el
# cerrojo de escritura se libera entre lotes).
DELETE_CHUNK_ROWS = 50_000

# VACUUM periódico opcional. Devolver espacio al SO es delicado:
#   * ``PRAGMA incremental_vacuum`` solo funciona si la base se creó con
#     auto_vacuum=INCREMENTAL (retention.py lo activa en bases nuevas). Es
#     barato y solo requiere el cerrojo de escritura por tandas de páginas.
#   * ``VACUUM`` completo reutiliza/copía toda la base: bloquea las escrituras
#     durante toda la copia y necesita hasta 2x el tamaño del archivo en disco
#     libre. Por eso es opt-in (MONITOREO_VACUUM_FULL=1 / --vacuum-full).
# Intervalo en segundos entre intentos; 0 desactiva el paso. Cada intento
# incremental fallido (auto_vacuum no activo) solo deja un log informativo.
RETENTION_VACUUM_INTERVAL_S = _env_int("MONITOREO_VACUUM_INTERVAL_S", 86_400)
RETENTION_VACUUM_FULL = _env_bool("MONITOREO_VACUUM_FULL", False)

# ---------------------------------------------------------------------------
# Constantes físicas y de filtrado (contrato §5)
# ---------------------------------------------------------------------------
GRAVITY = 9.80665  # m/s², definido por el contrato
# Filtro complementario: α = 0.96 es el peso de la integración del giroscopio;
# el 4 % restante corrige la deriva con la inclinación del acelerómetro.
# Es exactamente el α del visualizador 3D en Processing (ver
# ../processing/README.md) para que ambos visores coincidan.
COMPLEMENTARY_ALPHA = 0.96
# Calibración del bias del giroscopio: ~1 s en reposo = 50 muestras a 50 Hz,
# igual que el mensaje "CALIBRANDO GIROSCOPIO" del visualizer.
GYRO_BIAS_SAMPLES = 50
# Si llega una muestra con un hueco mayor que esto no se integra el paso: un
# hueco no debe interpretarse como una rotación enorme (misma regla que el
# visualizer, que reinicia el filtro a los 500 ms).
FILTER_MAX_GAP_S = 0.5

# ---------------------------------------------------------------------------
# Identificadores de sensor y métricas (contrato §2.1, §4.2 y §5)
# ---------------------------------------------------------------------------
SENSOR_MPU6050 = "mpu6050"  # líneas "DATA,"
SENSOR_ADXL345 = "adxl345"  # líneas "ADXL,"
KNOWN_SENSORS: tuple[str, ...] = (SENSOR_MPU6050, SENSOR_ADXL345)

# Campos seriales del contrato §2.1 (el orden es el del protocolo).
DATA_FIELDS = ("accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z", "temp_c")
ADXL_FIELDS = ("accel_x", "accel_y", "accel_z")

# Columnas "raw" de la tabla samples (contrato §3). gyro_* siempre en rad/s.
RAW_COLUMNS = ("accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z", "temp_c")

# Métricas derivadas por sensor (contrato §5):
#   roll/pitch/yaw -> solo MPU6050
#   vib_rms/vib_peak -> MPU6050 y ADXL345
#   accel_mag -> todos los acelerómetros
#   gyro_mag -> MPU6050
RAW_METRICS: dict[str, tuple[str, ...]] = {
    SENSOR_MPU6050: RAW_COLUMNS,
    SENSOR_ADXL345: ("accel_x", "accel_y", "accel_z"),
}
DERIVED_METRICS: dict[str, tuple[str, ...]] = {
    SENSOR_MPU6050: (
        "accel_mag",
        "gyro_mag",
        "roll",
        "pitch",
        "yaw",
        "vib_rms",
        "vib_peak",
    ),
    SENSOR_ADXL345: ("accel_mag", "vib_rms", "vib_peak"),
}

# ---------------------------------------------------------------------------
# Simulador (--simulate): fuente de datos sintéticos para pruebas sin ESP32
# ---------------------------------------------------------------------------
SIM_TICK_HZ = 50  # misma cadencia que el firmware real
SIM_PORT_LABEL = "simulate"  # etiqueta del campo "port" del estado WebSocket
