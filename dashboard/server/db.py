"""Capa de persistencia: clase ``Repository`` sobre SQLite (contrato §3).

Por qué una capa de repositorio y no SQL repartido por el código: el contrato
prevé migrar a InfluxDB/PostgreSQL; concentrar el SQL aquí hace que ese cambio
no toque el parser, las derivadas ni la API.

Decisión sobre concurrencia (requisito de la tesis):

* SQLite en modo **WAL**: los lectores (API HTTP) no bloquean al escritor y
  viceversa, que es justo el patrón "hilo de ingesta escribe / HTTP lee".
* **Una conexión por hilo** (``threading.local``) con ``check_same_thread`` en
  su valor por defecto. Así cada endpoint y el hilo de ingesta usan su propia
  conexión sin cerrojos de Python para leer.
* Los **escritores** pasan además por un ``threading.Lock`` global: los INSERT
  van en lotes y se ejecutan como una única transacción, de modo que la API
  nunca observa lotes a medias y no se producen ``database is locked``.
* ``busy_timeout = 5000`` como red de seguridad si dos escritores (hilo serie
  y ``POST /api/ingest``) coinciden.

El esquema SQL es literalmente el del contrato: nombres de tablas, columnas y
tipos no se reinterpretan.
"""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path

from . import config
from .derived import DerivedEngine
from .protocol import LogEvent, Sample

log = logging.getLogger(__name__)

# Esquema textual del contrato §3. Se crea tal cual; IF NOT EXISTS hace que el
# arranque sea idempotente y seguro con una base de datos ya existente.
#
# Dos adiciones respecto al contrato, ambas al servicio de la retención
# (ver retention.py y la sección de retención de ARQUITECTURA.md):
#   * idx_samples_ts sobre samples(ts_ms): la purga por edad, el techo de
#     filas y MIN/MAX de ts consultan por tiempo sin pasar por el sensor. Sin
#     este índice cada purga sería un escaneo completo de la tabla.
#   * samples_agg: agregados por bucket (promedio/min/max/n). Es la tabla que
#     conserva las tendencias cuando el detalle se reduce.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS samples (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms     INTEGER NOT NULL,
  sensor    TEXT    NOT NULL,
  accel_x   REAL, accel_y REAL, accel_z REAL,
  gyro_x    REAL, gyro_y  REAL, gyro_z REAL,
  temp_c    REAL,
  payload   TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_sensor_ts ON samples(sensor, ts_ms);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts_ms);

CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms     INTEGER NOT NULL,
  sensor    TEXT,
  level     TEXT NOT NULL,
  code      TEXT,
  message   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sensor_status (
  sensor     TEXT PRIMARY KEY,
  present    INTEGER NOT NULL DEFAULT 0,
  last_ts_ms INTEGER,
  n_samples  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS samples_agg (
  bucket_ts_ms INTEGER NOT NULL,
  sensor TEXT NOT NULL,
  metric TEXT NOT NULL,
  n INTEGER NOT NULL,
  avg REAL, min REAL, max REAL,
  PRIMARY KEY (bucket_ts_ms, sensor, metric)
);
"""

_INSERT_SAMPLE_SQL = """
INSERT INTO samples
  (ts_ms, sensor, accel_x, accel_y, accel_z, gyro_x, gyro_y, gyro_z, temp_c, payload)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_EVENT_SQL = """
INSERT INTO events (ts_ms, sensor, level, code, message)
VALUES (?, ?, ?, ?, ?)
"""

# Upsert con acumulación de n_samples: recibe incrementos, no totales, para
# que dos hilos ingentes no se pisen al escribir el contador.
_UPSERT_STATUS_SQL = """
INSERT INTO sensor_status (sensor, present, last_ts_ms, n_samples)
VALUES (?, ?, ?, ?)
ON CONFLICT(sensor) DO UPDATE SET
  present    = excluded.present,
  last_ts_ms = MAX(COALESCE(sensor_status.last_ts_ms, 0),
                   COALESCE(excluded.last_ts_ms, 0)),
  n_samples  = sensor_status.n_samples + excluded.n_samples
"""

# Upsert de agregados con fusión. Si un mismo bucket vuelve a agregarse (una
# llegada tardía, o un corte por techo de filas a mitad de bucket), las
# estadísticas se FUSIONAN en lugar de sobrescribirse: n se suma, el promedio
# pasa a ser el de la unión (media ponderada por n) y min/max se combinan.
# Así el orden de ejecución de las pasadas de retención no cambia el resultado.
_UPSERT_AGG_SQL = """
INSERT INTO samples_agg (bucket_ts_ms, sensor, metric, n, avg, min, max)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(bucket_ts_ms, sensor, metric) DO UPDATE SET
  n   = samples_agg.n + excluded.n,
  avg = (samples_agg.avg * samples_agg.n + excluded.avg * excluded.n)
        / (samples_agg.n + excluded.n),
  min = MIN(samples_agg.min, excluded.min),
  max = MAX(samples_agg.max, excluded.max)
"""

# Columnas exportables tal cual (el payload JSON no se exporta: es ruido para
# el análisis estadístico y ocupa más que las propias medidas).
_CSV_COLUMNS = (
    "id",
    "ts_ms",
    "sensor",
    "accel_x",
    "accel_y",
    "accel_z",
    "gyro_x",
    "gyro_y",
    "gyro_z",
    "temp_c",
)


class Repository:
    """Acceso a ``data/monitoreo.db`` tras la interfaz que usará el sistema."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else config.DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._write_lock = threading.Lock()  # serializa transacciones de escritura

        # La creación del esquema se hace en el hilo principal, al arrancar.
        self.init_schema()
        self._seed_sensor_status()

    # ------------------------------------------------------------------
    # Conexiones
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Conexión SQLite propia del hilo llamante (patrón documentado arriba)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            # auto_vacuum=INCREMENTAL solo puede fijarse mientras la base está
            # vacía (sin tablas) y ANTES de cualquier escritura de encabezado:
            # `journal_mode=WAL` deja el valor ya fijado, así que este PRAGMA
            # tiene que ir el primero de todos. Con él, incremental_vacuum
            # puede devolver páginas al SO (retention.py); sin él, los DELETE
            # solo dejan páginas reutilizables y el archivo nunca se encoge.
            # En una base ya poblada es un no-op inocuo.
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table'"
            ).fetchone()
            if int(row["n"]) == 0:
                conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            # WAL: lecturas concurrentes con escrituras; escribe un solo
            # writer sin bloquear a los endpoints del dashboard.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
            with self._conns_lock:
                self._all_conns.append(conn)
        return conn

    def close_all(self) -> None:
        """Cierra todas las conexiones creadas (parada limpia del servidor)."""
        with self._conns_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - cierre defensivo
                pass

    # ------------------------------------------------------------------
    # Esquema
    # ------------------------------------------------------------------

    def init_schema(self) -> None:
        """Crea tablas e índices del contrato §3 si no existen.

        (El PRAGMA auto_vacuum de las bases nuevas vive en :meth:`_conn`, no
        aquí: debe ejecutarse antes de que el modo WAL escriba el encabezado.)
        """
        conn = self._conn()
        with self._write_lock:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
        log.debug("Esquema SQLite verificado en %s", self.db_path)

    def _seed_sensor_status(self) -> None:
        """Prepara una fila por sensor conocido.

        El contrato §4.2 ejemplifica el estado con mpu6050 y adxl345 siempre
        presentes en el objeto ``sensors``; sembrar las filas garantiza que
        aparezcan incluso antes de la primera muestra.
        """
        conn = self._conn()
        with self._write_lock:
            for sensor in config.KNOWN_SENSORS:
                conn.execute(
                    "INSERT OR IGNORE INTO sensor_status (sensor, present)"
                    " VALUES (?, 0)",
                    (sensor,),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Escritura
    # ------------------------------------------------------------------

    def insert_samples(self, samples: Sequence[Sample]) -> int:
        """Inserta un lote de muestras en una única transacción.

        Devuelve el número de filas escritas. Un lote = una transacción: es la
        unidad que mantiene el hilo de ingesta por debajo de los 200 ms con
        50 Hz de datos (contrato §6).
        """
        if not samples:
            return 0
        rows = [s.as_row() for s in samples]
        conn = self._conn()
        with self._write_lock:
            conn.executemany(_INSERT_SAMPLE_SQL, rows)
            conn.commit()
        return len(rows)

    def insert_event(self, event: LogEvent) -> None:
        """Registra un evento de diagnóstico (contrato §2.2)."""
        conn = self._conn()
        with self._write_lock:
            conn.execute(_INSERT_EVENT_SQL, event.as_row())
            conn.commit()

    def upsert_sensor_status(
        self,
        sensor: str,
        present: bool,
        last_ts_ms: int | None,
        n_delta: int = 0,
    ) -> None:
        """Actualiza el estado de un sensor acumulando ``n_delta`` muestras.

        ``n_delta`` (y no un total absoluto) evita carreras entre el hilo de
        ingesta y el endpoint HTTP: cada uno suma lo suyo y el SQL acumula.
        """
        conn = self._conn()
        with self._write_lock:
            conn.execute(
                _UPSERT_STATUS_SQL,
                (sensor, 1 if present else 0, last_ts_ms, max(0, int(n_delta))),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------

    def get_sensor_status_rows(self) -> dict[str, dict]:
        """Estado persistido por sensor, para arrancar los contadores.

        ``present`` no se reutiliza al arrancar: la presencia real se decide
        por frescura de datos (2 s, contrato §2.3), no por lo que quedó
        escrito en la sesión anterior.
        """
        rows = self._conn().execute(
            "SELECT sensor, present, last_ts_ms, n_samples FROM sensor_status"
        ).fetchall()
        return {
            row["sensor"]: {
                "present": bool(row["present"]),
                "last_ts_ms": row["last_ts_ms"],
                "n_samples": row["n_samples"],
            }
            for row in rows
        }

    def query_history(
        self,
        sensor: str,
        metrics: Sequence[str] | None,
        from_ms: int,
        to_ms: int,
        limit: int,
    ) -> dict:
        """Series históricas con la forma de respuesta del contrato §4.1.

        ``metrics=None`` es el defecto del contrato (parámetro ``metrics`` =
        "todos"): devuelve **todas** las métricas del sensor, las crudas y las
        derivadas. Admite métricas ``raw`` (columnas de ``samples``) y métricas
        derivadas. Las derivadas no están en la base de datos porque el
        contrato fija el esquema SQL: se **recomputan releyendo las muestras
        y pasándolas por :class:`DerivedEngine`** (con 2 s de calentamiento
        previo para que el filtro complemente como en vivo). Es la misma capa
        de derivadas que el WebSocket, luego los valores coinciden por
        construcción.
        """
        raw_allowed = config.RAW_METRICS.get(sensor, config.RAW_COLUMNS)
        derived_allowed = config.DERIVED_METRICS.get(sensor, ())

        if metrics is None:
            # Defecto del contrato §4.1: `metrics` es "todos", y §5 define las
            # métricas derivadas como métricas del sistema (accel_mag, gyro_mag,
            # roll/pitch/yaw, vib_rms, vib_peak). Por tanto "todos" = crudas +
            # derivadas del sensor: limitar la respuesta a las columnas raw
            # dejaba vib_rms/vib_peak fuera y la gráfica de vibración del
            # dashboard arrancaba vacía tras cada recarga del histórico.
            requested = list(raw_allowed) + [m for m in derived_allowed]
        else:
            requested = list(metrics)
            for name in requested:
                if name not in raw_allowed and name not in derived_allowed:
                    raise ValueError(f"métrica desconocida para {sensor}: {name!r}")

        raw_needed = [m for m in requested if m in config.RAW_COLUMNS]
        derived_needed = [m for m in requested if m in derived_allowed]

        # Las derivadas necesitan las columnas crudas para poder recalcularse.
        select_cols = list(config.RAW_COLUMNS)
        warmup_ms = 2000
        query_from = from_ms - warmup_ms if derived_needed else from_ms

        conn = self._conn()
        sql = (
            "SELECT ts_ms, " + ", ".join(select_cols) + " FROM samples"
            " WHERE sensor = ? AND ts_ms >= ? AND ts_ms <= ?"
            " ORDER BY ts_ms DESC LIMIT ?"
        )
        # Se descuentan primero las filas de calentamiento (~100 a 50 Hz) para
        # que no consuman el presupuesto del límite; el recorte final garantiza
        # que nunca se devuelvan más de ``limit`` puntos.
        warmup_rows = 200 if derived_needed else 0
        rows = conn.execute(
            sql, (sensor, query_from, to_ms, limit + warmup_rows)
        ).fetchall()
        rows.reverse()  # el contrato muestra la serie en orden cronológico

        # Filas visibles (dentro de la ventana pedida). Si exceden el límite
        # se conservan las más recientes: es lo útil para un dashboard que
        # pide "los últimos N puntos de la ventana".
        visible = [row for row in rows if row["ts_ms"] >= from_ms]
        warmup = [row for row in rows if row["ts_ms"] < from_ms]
        if len(visible) > limit:
            visible = visible[-limit:]

        series: dict[str, list[list]] = {name: [] for name in requested}
        count = len(visible)

        # ---- métricas raw ------------------------------------------------
        # Se descartan los None: un ADXL345 no tiene gyro_* y no debe
        # inventar puntos para esas métricas.
        for row in visible:
            for name in raw_needed:
                value = row[name]
                if value is not None:
                    series[name].append([row["ts_ms"], value])

        # ---- métricas derivadas (replay del motor) -----------------------
        if derived_needed:
            engine = DerivedEngine(sensor)
            for row in warmup + visible:
                sample = Sample(
                    ts_ms=row["ts_ms"],
                    sensor=sensor,
                    accel_x=row["accel_x"] or 0.0,
                    accel_y=row["accel_y"] or 0.0,
                    accel_z=row["accel_z"] or 0.0,
                    gyro_x=row["gyro_x"],
                    gyro_y=row["gyro_y"],
                    gyro_z=row["gyro_z"],
                    temp_c=row["temp_c"],
                )
                derived = engine.update(sample)
                if row["ts_ms"] < from_ms:
                    continue  # fila de calentamiento: solo alimenta el filtro
                for name in derived_needed:
                    if name in derived:
                        series[name].append([row["ts_ms"], derived[name]])

        return {
            "sensor": sensor,
            "from_ms": from_ms,
            "to_ms": to_ms,
            "count": count,
            "series": series,
        }

    def iter_csv(
        self,
        sensor: str | None = None,
        from_ms: int | None = None,
        to_ms: int | None = None,
        chunk: int = 500,
    ) -> Iterator[str]:
        """Genera el CSV de exportación (contrato §4.1 ``/api/export.csv``).

        Se itera por lotes con ``fetchmany`` en lugar de ``fetchall``: una
        prueba de campo puede dejar millones de filas en la base de datos y la
        descarga debe poderse hacer sin cargarla en memoria.

        Dos decisiones defensivas contra las descargas abortadas (``curl | head``,
        cancelación en el navegador). Un cliente que se va a mitad de stream
        abandona este generador con el SELECT aún abierto; sin más, ese cursor
        conserva su snapshot WAL y la conexión thread-local —compartida con
        ``/api/history``— queda sirviendo datos congelados durante minutos en el
        mismo hilo del threadpool:

        1. **Conexión propia de vida corta**, no la thread-local de lecturas:
           el snapshot del abort solo puede quedar clavado en una conexión que
           nadie más usa.
        2. **``try/finally``** que cierra cursor y conexión aunque el cliente
           aborte y el generador quede abandonado: el cierre libera la
           transacción de lectura en cuanto el generador se finaliza (incluido
           el ``GeneratorExit`` del cierre por ``BackgroundTask`` en app.py).
        """
        where: list[str] = []
        params: list = []
        if sensor:
            where.append("sensor = ?")
            params.append(sensor)
        if from_ms is not None:
            where.append("ts_ms >= ?")
            params.append(from_ms)
        if to_ms is not None:
            where.append("ts_ms <= ?")
            params.append(to_ms)

        sql = "SELECT " + ", ".join(_CSV_COLUMNS) + " FROM samples"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts_ms"

        # Conexión dedicada a esta exportación. ``isolation_level=None`` =
        # autocommit: la exportación es solo lectura y así cada SELECT vive en
        # su propia transacción de lectura, sin transacciones implícitas que
        # puedan quedar abiertas si algo se interrumpe.
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        conn.execute("PRAGMA busy_timeout=5000")

        cursor = None
        try:
            cursor = conn.execute(sql, params)

            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(_CSV_COLUMNS)
            yield buf.getvalue()

            while True:
                rows = cursor.fetchmany(chunk)
                if not rows:
                    break
                buf.seek(0)
                buf.truncate()
                for row in rows:
                    writer.writerow([row[col] for col in _CSV_COLUMNS])
                yield buf.getvalue()
        finally:
            # Se ejecuta también cuando el generador se cierra a medias
            # (abort del cliente). Cerrar el cursor finaliza el statement y,
            # con él, el snapshot de lectura; cerrar la conexión deja la de la
            # exportación sin recursos que retener.
            try:
                if cursor is not None:
                    cursor.close()
            except sqlite3.Error:  # pragma: no cover - cierre defensivo
                pass
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - cierre defensivo
                pass

    def count_samples(self) -> int:
        """Total de muestras almacenadas (comprobación de salud)."""
        row = self._conn().execute("SELECT COUNT(*) AS n FROM samples").fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # Retención, agregación y estado del almacén (retention.py)
    # ------------------------------------------------------------------
    # Por qué estas funciones viven en el repositorio y no en retention.py:
    # todo el SQL del sistema está concentrado aquí (decisión documentada en
    # el módulo), de modo que una futura migración a InfluxDB/TimescaleDB solo
    # reimplementa esta clase. retention.py orquesta; db.py sabe de SQL.

    def downsample_before(self, cutoff_ms: int, bucket_ms: int) -> tuple[int, int]:
        """Agrega y elimina el detalle con ``ts_ms < cutoff_ms``.

        Devuelve ``(filas_detalle_procesadas, filas_escritas_en_samples_agg)``.
        El detalle se borra DESPUÉS de agregarse, en la misma transacción por
        ventana: nunca se pierde información sin dejar su agregado detrás, ni
        se duplica (la ventana y el borrado comparten exactamente los mismos
        límites de tiempo).

        La ventana va de MIN(ts_ms) a ``cutoff_ms`` en tandas de
        ``AGGREGATE_CHUNK_BUCKETS`` buckets. Cada tanda es una transacción que
        libera el cerrojo de escritura al terminar: la ingesta a 50 Hz
        (contrato §6: la ingesta no debe bloquearse) sigue escribiendo entre
        tandas. Una muestra que llegue a un bucket ya procesado se agregará en
        una pasada posterior y el upsert fusionará sus estadísticas.
        """
        if cutoff_ms <= 0 or bucket_ms <= 0:
            return 0, 0
        conn = self._conn()
        row = conn.execute(
            "SELECT MIN(ts_ms) AS t FROM samples WHERE ts_ms < ?", (cutoff_ms,)
        ).fetchone()
        if row is None or row["t"] is None:
            return 0, 0

        total_rows = 0
        total_agg = 0
        # Se alinea el arranque al bucket para no partir la primera ventana.
        t0 = (int(row["t"]) // bucket_ms) * bucket_ms
        window = bucket_ms * config.AGGREGATE_CHUNK_BUCKETS
        while t0 < cutoff_ms:
            t1 = min(t0 + window, cutoff_ms)
            rows, agg_rows = self._downsample_window(t0, t1, bucket_ms)
            total_rows += rows
            total_agg += agg_rows
            t0 = t1
        return total_rows, total_agg

    def _downsample_window(self, t0: int, t1: int, bucket_ms: int) -> tuple[int, int]:
        """Una ventana ``[t0, t1)``: agrega, escribe ``samples_agg`` y borra.

        El cálculo es por (bucket, sensor) con AVG/MIN/MAX/COUNT nativos de
        SQLite: procesar millones de filas en Python sería inaceptablemente
        lento. Las métricas agregadas son las columnas raw del sensor (las
        derivadas viven solo en memoria, ver §5 del contrato y derived.py);
        una métrica con ``n = 0`` (el giroscopio de un ADXL345) no genera fila.
        """
        cols = config.RAW_COLUMNS
        parts: list[str] = ["COUNT(*) AS n_rows"]
        for name in cols:
            parts.append(f"COUNT({name}) AS n_{name}")
            parts.append(f"AVG({name}) AS avg_{name}")
            parts.append(f"MIN({name}) AS min_{name}")
            parts.append(f"MAX({name}) AS max_{name}")
        # CAST por seguridad: ts_ms y el bucket son enteros y la división de
        # enteros de SQLite trunca hacia cero (ts_ms siempre es positivo).
        sql = (
            "SELECT CAST(ts_ms / ? AS INTEGER) * ? AS bucket_ts_ms, sensor, "
            + ", ".join(parts)
            + " FROM samples WHERE ts_ms >= ? AND ts_ms < ?"
            " GROUP BY bucket_ts_ms, sensor"
        )
        conn = self._conn()
        with self._write_lock:
            groups = conn.execute(sql, (bucket_ms, bucket_ms, t0, t1)).fetchall()
            agg_rows: list[tuple] = []
            for group in groups:
                sensor = group["sensor"]
                metrics = config.RAW_METRICS.get(sensor, config.RAW_COLUMNS)
                for name in metrics:
                    n = int(group[f"n_{name}"] or 0)
                    if n <= 0:
                        continue
                    agg_rows.append(
                        (
                            int(group["bucket_ts_ms"]),
                            sensor,
                            name,
                            n,
                            group[f"avg_{name}"],
                            group[f"min_{name}"],
                            group[f"max_{name}"],
                        )
                    )
            if agg_rows:
                conn.executemany(_UPSERT_AGG_SQL, agg_rows)
            cursor = conn.execute(
                "DELETE FROM samples WHERE ts_ms >= ? AND ts_ms < ?", (t0, t1)
            )
            deleted = int(cursor.rowcount or 0)
            conn.commit()
        return deleted, len(agg_rows)

    def purge_older_than(self, cutoff_ms: int, chunk_rows: int | None = None) -> int:
        """Borra el detalle con ``ts_ms < cutoff_ms``. Devuelve filas borradas.

        NO agrega nada: es la primitiva de retención por edad pura. La
        orquestación (agregar antes de borrar) es responsabilidad de
        retention.py; aquí solo se garantiza el borrado acotado en el tiempo.
        Se borra por tandas para no retener el cerrojo de escritura durante un
        DELETE masivo: entre tandas la ingesta sigue insertando.
        """
        if cutoff_ms <= 0:
            return 0
        chunk = chunk_rows or config.DELETE_CHUNK_ROWS
        conn = self._conn()
        total = 0
        while True:
            with self._write_lock:
                cursor = conn.execute(
                    "DELETE FROM samples WHERE id IN ("
                    "  SELECT id FROM samples WHERE ts_ms < ? LIMIT ?)",
                    (cutoff_ms, chunk),
                )
                deleted = int(cursor.rowcount or 0)
                conn.commit()
            total += deleted
            if deleted < chunk:
                return total

    def oldest_ts_boundary(self, n: int) -> int | None:
        """``ts_ms`` del n-ésimo registro más antiguo (1-based), o ``None``.

        Es el límite del techo de filas: todo registro con ``ts_ms`` menor o
        igual es candidato a expulsarse cuando ``n`` = filas en exceso. Con
        empates de ``ts_ms`` el recorte puede ser algo mayor que el exceso
        (se expulsa el instante completo): previsible y documentado.
        """
        if n <= 0:
            return None
        row = self._conn().execute(
            "SELECT ts_ms FROM samples ORDER BY ts_ms LIMIT 1 OFFSET ?", (n - 1,)
        ).fetchone()
        return None if row is None else int(row["ts_ms"])

    def stats(self) -> dict:
        """Estado del almacén para ``GET /api/db/stats``.

        ``est_daily_bytes`` es una estimación grosera del crecimiento diario:
        filas de la ventana más reciente (como mucho 1 día) extrapoladas a 24 h
        y valoradas al tamaño medio por fila observado (páginas en uso / filas,
        ver la nota en el cálculo). Sirve para que el operador vea la pendiente
        de crecimiento, no como factura.
        """
        conn = self._conn()
        samples_rows = int(
            conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"]
        )
        agg_rows = int(
            conn.execute("SELECT COUNT(*) AS n FROM samples_agg").fetchone()["n"]
        )
        detail = conn.execute(
            "SELECT MIN(ts_ms) AS oldest, MAX(ts_ms) AS newest FROM samples"
        ).fetchone()
        agg = conn.execute(
            "SELECT MIN(bucket_ts_ms) AS oldest, MAX(bucket_ts_ms) AS newest"
            " FROM samples_agg"
        ).fetchone()

        file_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        # El espacio en disco real incluye el WAL: con escrituras en vuelo el
        # -wal puede ser de varios MB y también presiona al disco.
        wal_path = Path(str(self.db_path) + "-wal")
        wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0

        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        freelist_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])

        oldest = int(detail["oldest"]) if detail["oldest"] is not None else None
        newest = int(detail["newest"]) if detail["newest"] is not None else None
        est_daily_bytes = 0
        if oldest is not None and newest is not None and newest > oldest:
            window_ms = min(newest - oldest, 86_400_000)
            recent = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM samples WHERE ts_ms >= ?",
                    (newest - window_ms,),
                ).fetchone()["n"]
            )
            rows_per_day = recent * 86_400_000 / window_ms
            total_rows = max(1, samples_rows + agg_rows)
            # Espacio realmente ocupado = páginas en uso x tamaño de página.
            # Ni el WAL (trabajo transitorio) ni las páginas libres cuentan:
            # justo tras una purga el archivo conserva huecos reutilizables y
            # medir "bytes por fila" sobre el tamaño del archivo los repartiría
            # entre las filas supervivientes, inflando la estimación.
            occupied_bytes = max(0, (page_count - freelist_pages) * page_size)
            bytes_per_row = occupied_bytes / total_rows
            est_daily_bytes = int(rows_per_day * bytes_per_row)

        return {
            "file_bytes": file_bytes,
            "wal_bytes": wal_bytes,
            "samples_rows": samples_rows,
            "samples_agg_rows": agg_rows,
            "oldest_ts_ms": oldest,
            "newest_ts_ms": newest,
            "oldest_agg_ts_ms": int(agg["oldest"]) if agg["oldest"] is not None else None,
            "newest_agg_ts_ms": int(agg["newest"]) if agg["newest"] is not None else None,
            "page_size": page_size,
            "freelist_pages": freelist_pages,
            "freelist_bytes": freelist_pages * page_size,
            "est_daily_bytes": est_daily_bytes,
        }

    # -- mantenimiento del archivo (devolver espacio al SO) ---------------

    def auto_vacuum_mode(self) -> int:
        """0 = none, 1 = full, 2 = incremental (PRAGMA auto_vacuum)."""
        return int(self._conn().execute("PRAGMA auto_vacuum").fetchone()[0])

    def incremental_vacuum(self, pages: int) -> None:
        """Devuelve hasta ``pages`` páginas libres al final del archivo.

        Requiere auto_vacuum=INCREMENTAL (se activa al crear la base nueva en
        :meth:`init_schema`). Bloquea las escrituras solo mientras retira las
        páginas de esa tanda.
        """
        with self._write_lock:
            self._conn().execute(f"PRAGMA incremental_vacuum({int(pages)})")
            self._conn().commit()

    def full_vacuum(self) -> None:
        """VACUUM completo: compacta el archivo y lo devuelve a su tamaño mínimo.

        PRECAUCIÓN: copia la base entera, por lo que bloquea TODAS las
        escrituras durante la copia y necesita hasta 2x el tamaño del archivo
        en disco libre. Solo debe ejecutarse con tráfico parado o en ventana de
        mantenimiento (retention.py lo hace opt-in por esta razón).
        """
        with self._write_lock:
            self._conn().execute("VACUUM")
            self._conn().commit()

    def freelist_pages(self) -> int:
        """Páginas marcadas como libres (reutilizables, no devueltas al SO)."""
        return int(self._conn().execute("PRAGMA freelist_count").fetchone()[0])
