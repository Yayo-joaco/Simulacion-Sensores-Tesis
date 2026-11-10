"""Aplicación FastAPI: REST + WebSocket del contrato §4 y frontend estático.

Decisiones de diseño:

* **Separación de planos**: las rutas que tocan SQLite se declaran como
  funciones síncronas (``def``), de modo que Starlette las ejecuta en su
  threadpool y una consulta lenta nunca bloquea el event loop que sirve los
  WebSocket a 100 msg/s (contrato §6).
* **Fan-out de WebSocket** en una única tarea consumidora de cola: el hilo de
  ingesta publica en una ``asyncio.Queue`` de forma segura entre hilos y una
  tarea del event loop reparte a los clientes. Un cliente lento se descarta en
  vez de frenar a los demás.
* **Estático al final**: ``web/`` se monta en "/" después de declarar las
  rutas API para que Starlette resuelva primero /api/* y /ws. Si el frontend
  aún no existe, "/" responde una página mínima en lugar de un 404, para que
  el servidor sea utilizable y testeable por separado.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import config
from .db import Repository
from .ingest import IngestService
from .protocol import ProtocolParser, Sample
from .retention import RetentionPolicy

log = logging.getLogger(__name__)

# Página mínima para "/" cuando web/index.html aún no está desplegado. Es
# texto plano generado en memoria: no se escribe ningún archivo en web/ para
# no pisar el trabajo del frontend.
_PLACEHOLDER_HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>Monitoreo estructural</title></head>
<body style="font-family: sans-serif; margin: 3rem;">
<h1>Servidor de ingesta operativo</h1>
<p>El dashboard web no est&aacute; instalado a&uacute;n (falta
<code>web/index.html</code>). La API responde con normalidad:</p>
<ul>
<li><a href="/api/status">/api/status</a></li>
<li><a href="/api/sensors">/api/sensors</a></li>
<li><a href="/api/db/stats">/api/db/stats</a></li>
<li><a href="/api/health">/api/health</a></li>
</ul></body></html>
"""


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------


class IngestBody(BaseModel):
    """Cuerpo de ``POST /api/ingest`` tal como lo define el contrato §4.1.

    ``ts_ms`` es opcional (si falta se usa la hora de recepción) y los campos
    de giroscopio/temperatura también, para poder ingestar muestras de un
    ADXL345 remoto sin inventar valores.
    """

    sensor: str
    ts_ms: int | None = None
    accel_x: float
    accel_y: float
    accel_z: float
    gyro_x: float | None = None
    gyro_y: float | None = None
    gyro_z: float | None = None
    temp_c: float | None = None

    def all_finite(self) -> bool:
        """El contrato §2.1 descarta valores no finitos; aquí se rechazan."""
        values = [
            self.accel_x,
            self.accel_y,
            self.accel_z,
            self.gyro_x,
            self.gyro_y,
            self.gyro_z,
            self.temp_c,
        ]
        return all(v is None or math.isfinite(v) for v in values)


# ---------------------------------------------------------------------------
# Clientes WebSocket
# ---------------------------------------------------------------------------


class ClientManager:
    """Gestiona los clientes de ``/ws`` y el reparto de mensajes.

    El método :meth:`publish` es el único punto de entrada desde otros hilos
    (hilo de ingesta, threadpool de endpoints) y delega en
    ``call_soon_threadsafe``: es la forma canónica de tocar el event loop de
    asyncio desde un hilo sin datos de carrera.
    """

    def __init__(self, max_queue: int = 4000) -> None:
        self._clients: set[WebSocket] = set()
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=max_queue)
        self._loop: asyncio.AbstractEventLoop | None = None
        self.dropped = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # -- lado hilos --------------------------------------------------------

    def publish(self, message: dict) -> None:
        """Publica un mensaje para todos los clientes (válido desde hilos)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._enqueue, message)
        except RuntimeError:
            # El loop se está cerrando: no hay nada que publicar.
            pass

    def _enqueue(self, message: dict) -> None:
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            # Si el dashboard no da abasto se descartan mensajes en vivo; el
            # histórico sigue disponible en /api/history.
            self.dropped += 1

    # -- lado event loop --------------------------------------------------

    async def add(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def run(self) -> None:
        """Tarea de reparto: un único emisor, envíos con timeout por cliente."""
        while True:
            message = await self._queue.get()
            data = json.dumps(message, ensure_ascii=False)
            for ws in list(self._clients):
                try:
                    # Timeout para que un cliente congelado no atore el reparto
                    # al resto; si no responde a tiempo se le descarta.
                    await asyncio.wait_for(ws.send_text(data), timeout=2.0)
                except Exception:
                    await self.remove(ws)
                    try:
                        await ws.close()
                    except Exception:  # pragma: no cover - cierre defensivo
                        pass


# ---------------------------------------------------------------------------
# Fábrica de la aplicación
# ---------------------------------------------------------------------------


def create_app(
    simulate: bool = False,
    serial_port: str = config.SERIAL_PORT,
    db_path=None,
    retention_days: int | None = None,
    retention_max_rows: int | None = None,
    aggregate_after_days: int | None = None,
    aggregate_bucket_min: int | None = None,
    retention_interval_s: float | None = None,
    vacuum_interval_s: float | None = None,
    vacuum_full: bool | None = None,
) -> FastAPI:
    """Construye la aplicación. El arranque real ocurre en el ``lifespan``.

    Los parámetros de retención son opcionales: ``None`` significa "el valor de
    ``config``" (que a su vez sale de las variables de entorno
    ``MONITOREO_*``). ``run.py`` los pasa cuando el operador usa banderas.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repo = Repository(db_path)
        clients = ClientManager()
        clients.bind_loop(asyncio.get_running_loop())

        ingest = IngestService(
            repo=repo,
            on_message=clients.publish,
            serial_port=serial_port,
            simulate=simulate,
            parser=ProtocolParser(),
        )
        retention = RetentionPolicy(
            repo,
            retention_days=retention_days,
            max_rows=retention_max_rows,
            aggregate_after_days=aggregate_after_days,
            bucket_min=aggregate_bucket_min,
            interval_s=retention_interval_s,
            vacuum_interval_s=vacuum_interval_s,
            vacuum_full=vacuum_full,
        )
        broadcaster = asyncio.create_task(clients.run(), name="ws-broadcast")
        # Tarea de fondo de retención (agregación + purga + vacuum). Arranca
        # una pasada inmediata y después una cada RETENTION_INTERVAL_S; su
        # trabajo pesado va a un hilo del pool para no bloquear WebSocket.
        retention_task = asyncio.create_task(
            retention.run_loop(), name="retention"
        )
        ingest.start()

        app.state.repo = repo
        app.state.ingest = ingest
        app.state.clients = clients
        app.state.retention = retention

        log.info(
            "Servidor listo (fuente de datos: %s)",
            "simulador" if simulate else serial_port,
        )
        try:
            yield
        finally:
            ingest.stop()
            # Primero la retención: una pasada en curso no debe tocar la base
            # mientras se cierran las conexiones (run_once tolera el cierre y
            # solo lo registra como error de paso).
            retention_task.cancel()
            try:
                await retention_task
            except asyncio.CancelledError:
                pass
            broadcaster.cancel()
            try:
                await broadcaster
            except asyncio.CancelledError:
                pass
            repo.close_all()

    app = FastAPI(
        title="Servidor de ingesta - Monitoreo estructural",
        description="Implementa CONTRATO_DE_DATOS.md (REST §4.1 y WebSocket §4.2).",
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------
    # REST (contrato §4.1). Orden de declaración relevante: el montaje del
    # estático en "/" se hace al final para no capturar estas rutas.
    # ------------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict:
        """Comprobación de vida: sin dependencias, responde siempre."""
        return {"status": "ok", "ts_ms": int(time.time() * 1000)}

    @app.get("/api/status")
    def status() -> dict:
        """Estado de ingesta y de cada sensor (contrato §4.1).

        Es un superconjunto del mensaje WebSocket ``status``: además de
        ``connected``/``port``/``sensors`` incluye contadores de diagnóstico
        (``parse_errors``, §2.3) que el contrato solo menciona como regla.
        """
        return app.state.ingest.status_dict()

    @app.get("/api/sensors")
    def sensors() -> dict:
        """Catálogo de sensores y métricas disponibles (contrato §5).

        La forma concreta de esta respuesta no viene fijada por el contrato;
        se expone como lista con ``raw_metrics`` y ``derived_metrics`` por
        sensor para que el dashboard pueda poblar sus selectores sin
        duplicar conocimiento del contrato.
        """
        return {
            "sensors": [
                {
                    "id": sensor,
                    "raw_metrics": list(config.RAW_METRICS[sensor]),
                    "derived_metrics": list(config.DERIVED_METRICS[sensor]),
                }
                for sensor in config.KNOWN_SENSORS
            ]
        }

    @app.get("/api/history")
    def history(
        sensor: str = Query(default=config.SENSOR_MPU6050),
        metrics: str | None = Query(default=None),
        seconds: int = Query(default=60, ge=1),
        limit: int = Query(default=5000, ge=1),
    ) -> dict:
        """Series históricas con la forma de respuesta del contrato §4.1."""
        metric_list = None
        if metrics:
            metric_list = [m.strip() for m in metrics.split(",") if m.strip()]
        to_ms = int(time.time() * 1000)
        from_ms = to_ms - seconds * 1000
        try:
            return app.state.repo.query_history(
                sensor=sensor,
                metrics=metric_list,
                from_ms=from_ms,
                to_ms=to_ms,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/db/stats")
    def db_stats() -> dict:
        """Estado del almacén: tamaño, filas, ventana temporal y política.

        Es lo que el operador necesita para VER dónde están sus datos y a qué
        ritmo crecen sin abrir SQLite a mano: filas de detalle y de agregados,
        edades, tamaño del archivo (y del WAL), páginas libres, estimación de
        crecimiento diario y cuándo toca la siguiente pasada de retención.
        La forma de la respuesta no la fija el contrato; se documenta en
        ARQUITECTURA.md y es aditiva (campos nuevos no rompen lectores).
        """
        stats = app.state.repo.stats()
        retention: RetentionPolicy = app.state.retention
        stats.update(
            {
                "retention_days": retention.retention_days,
                "max_rows": retention.max_rows,
                "aggregate_after_days": retention.aggregate_after_days,
                "aggregate_bucket_min": retention.bucket_min,
                "retention_interval_s": retention.interval_s,
                "next_purge_ts_ms": retention.next_purge_ts_ms,
                "last_purge": retention.last_summary,
            }
        )
        return stats

    @app.get("/api/export.csv")
    def export_csv(
        sensor: str | None = Query(default=None),
        seconds: int | None = Query(default=None, ge=1),
    ) -> StreamingResponse:
        """Descarga CSV para análisis posterior (contrato §4.1).

        Se transmite en streaming: una campaña larga puede ocupar millones de
        filas y no deben pasar todas por memoria. Columnas = esquema §3 sin el
        payload JSON (auditoría, no análisis).
        """
        to_ms = int(time.time() * 1000)
        from_ms = to_ms - seconds * 1000 if seconds is not None else None
        chunks = app.state.repo.iter_csv(sensor=sensor, from_ms=from_ms, to_ms=to_ms)
        filename = f"monitoreo_{int(time.time())}.csv"
        # El BackgroundTask cierra el generador cuando la respuesta termina,
        # también cuando el cliente aborta la descarga (curl | head, cancelación
        # en el navegador) y el StreamingResponse deja de iterar. Sin este
        # cierre explícito el generador quedaría suspendido hasta que el GC lo
        # libere; con él, el try/finally de Repository.iter_csv libera el
        # cursor y la conexión de exportación de inmediato.
        return StreamingResponse(
            chunks,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            background=BackgroundTask(chunks.close),
        )

    @app.post("/api/ingest")
    def ingest(body: IngestBody) -> dict:
        """Ingesta alternativa por red (Wi-Fi / pasarela), contrato §4.1.

        Comparte el mismo camino que el puerto serie: mismo parser de reglas
        (valores finitos), mismas derivadas, misma publicación por WebSocket.
        El contrato no fija el cuerpo de respuesta; se devuelve ``ok`` con las
        derivadas calculadas, útil para depurar una estación remota.
        """
        if body.sensor not in config.KNOWN_SENSORS:
            raise HTTPException(
                status_code=400, detail=f"sensor desconocido: {body.sensor!r}"
            )
        if not body.all_finite():
            raise HTTPException(
                status_code=400,
                detail="valores no finitos: la muestra se descarta (contrato §2.1)",
            )
        sample = Sample(
            ts_ms=body.ts_ms if body.ts_ms is not None else int(time.time() * 1000),
            sensor=body.sensor,
            accel_x=body.accel_x,
            accel_y=body.accel_y,
            accel_z=body.accel_z,
            gyro_x=body.gyro_x,
            gyro_y=body.gyro_y,
            gyro_z=body.gyro_z,
            temp_c=body.temp_c,
        )
        message = app.state.ingest.ingest_sample(sample)
        return {"ok": True, "derived": message["derived"]}

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        """Canal en vivo con los tres tipos de mensaje del contrato §4.2.

        Al conectar se envía el estado actual (regra del contrato: "el servidor
        emite status al conectar un cliente"); después solo se usa el socket
        para salir, la telemetría es unidireccional servidor → dashboard.
        """
        clients: ClientManager = app.state.clients
        await clients.add(ws)
        try:
            await ws.send_text(
                json.dumps(app.state.ingest.status_message(), ensure_ascii=False)
            )
            while True:
                # Se mantiene el socket abierto; el cliente no envía datos.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:  # pragma: no cover - cierre defensivo
            log.debug("WebSocket cerrado de forma inesperada", exc_info=True)
        finally:
            await clients.remove(ws)

    # ------------------------------------------------------------------
    # Frontend estático. Se declara lo último: "/" es un montaje y capturaría
    # las rutas anteriores si se declarara antes.
    # ------------------------------------------------------------------

    config.WEB_DIR.mkdir(parents=True, exist_ok=True)

    @app.get("/", include_in_schema=False)
    def index():
        index_file = config.WEB_DIR / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return HTMLResponse(_PLACEHOLDER_HTML)

    app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")

    return app


# Instancia por defecto: la usa `uvicorn server.app:app` y el chequeo de
# importación. No arranca nada hasta que FastAPI dispare el lifespan.
app = create_app()
