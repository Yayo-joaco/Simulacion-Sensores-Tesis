"""Pruebas de la política de retención (retention.py) y del estado de la BD.

Por qué existen: el crecimiento de ``samples`` es el problema operativo más
grave del despliegue rural (~2,4 GB/día sin retención; ver config.py). Estas
pruebas fijan el contrato de comportamiento de los tres mecanismos (agregación
por bucket, retención por edad y techo de filas) y del endpoint de
visibilidad ``GET /api/db/stats``, de modo que una futura optimización no
pueda cambiar las reglas sin romper algo visible.

Se ejecutan con pytest desde dashboard/:

    .venv/bin/python -m pytest tests/ -q

La prueba de ``/api/db/stats`` levanta un servidor uvicorn real (patrón ya
usado en test_export_abort.py), lo que exige permitir el bind en 127.0.0.1.
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
from pathlib import Path

import pytest

# El test se ejecuta desde tests/; el paquete `server` es hermano de este
# directorio dentro de dashboard/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.db import Repository  # noqa: E402
from server.protocol import Sample  # noqa: E402
from server.retention import RetentionPolicy  # noqa: E402

DAY_MS = 86_400_000
BUCKET_MS = 5 * 60_000  # el bucket por defecto de la política (5 min)


def _sample(ts_ms: int, sensor: str = "mpu6050", accel_x: float = 0.5) -> Sample:
    return Sample(
        ts_ms=int(ts_ms),
        sensor=sensor,
        accel_x=accel_x,
        accel_y=1.0,
        accel_z=9.8,
        gyro_x=0.01,
        gyro_y=0.02,
        gyro_z=0.03,
        temp_c=26.0,
    )


@pytest.fixture()
def repo(tmp_path):
    """Repositorio sobre una base temporal, cerrado al terminar cada test."""
    r = Repository(tmp_path / "monitoreo.db")
    yield r
    r.close_all()


def _policy(repo: Repository, **overrides) -> RetentionPolicy:
    """Política con valores explícitos por test.

    Por defecto ``aggregate_after_days`` y ``retention_days`` enormes y techo
    ilimitado: cada test activa solo el mecanismo que quiere comprobar.
    """
    params = dict(
        retention_days=3650,
        max_rows=10**9,
        aggregate_after_days=3650,
        bucket_min=5,
        interval_s=3600,
        vacuum_interval_s=0,
    )
    params.update(overrides)
    return RetentionPolicy(repo, **params)


# ---------------------------------------------------------------------------
# 1. Retención por edad
# ---------------------------------------------------------------------------


def test_age_purge_deletes_only_old_and_keeps_recent(repo):
    now = int(time.time() * 1000)
    old_ts = now - 40 * DAY_MS  # más viejo que el límite de 30 días
    mid_ts = now - 20 * DAY_MS  # viejo pero dentro de la retención
    new_ts = now - 60_000  # reciente
    repo.insert_samples([_sample(old_ts + i) for i in range(5)])
    repo.insert_samples([_sample(mid_ts + i) for i in range(7)])
    repo.insert_samples([_sample(new_ts + i) for i in range(3)])

    deleted = repo.purge_older_than(now - 30 * DAY_MS)

    assert deleted == 5, "solo deben borrarse las 5 muestras de hace 40 días"
    assert repo.count_samples() == 10
    rows = repo._conn().execute("SELECT MIN(ts_ms) AS t FROM samples").fetchone()
    assert rows["t"] == mid_ts, "lo más antiguo conservado debe ser lo de hace 20 días"


def test_age_purge_is_chunked_and_exact(repo):
    """La purga por tandas borra exactamente lo que toca, sin cortes raros."""
    now = int(time.time() * 1000)
    base = now - 60 * DAY_MS
    repo.insert_samples([_sample(base + i * 1000) for i in range(250)])
    repo.insert_samples([_sample(now - 1000 + i) for i in range(250)])

    deleted = repo.purge_older_than(now - 30 * DAY_MS, chunk_rows=100)

    assert deleted == 250
    assert repo.count_samples() == 250


# ---------------------------------------------------------------------------
# 2. Techo de filas
# ---------------------------------------------------------------------------


def test_row_cap_trims_oldest_first(repo):
    now = int(time.time() * 1000)
    base = now - 3_600_000  # todo reciente: solo el techo puede actuar
    n = 100
    repo.insert_samples([_sample(base + i * 1000) for i in range(n)])

    policy = _policy(repo, max_rows=50)
    summary = policy.run_once()

    assert summary["errors"] == []
    assert repo.count_samples() == 50, "el techo debe dejar exactamente 50 filas"
    ts = [
        row["ts_ms"]
        for row in repo._conn().execute("SELECT ts_ms FROM samples ORDER BY ts_ms")
    ]
    assert ts == [base + i * 1000 for i in range(50, n)], (
        "deben sobrevivir las 50 más recientes, en el mismo estado de antes"
    )
    assert summary["purged_by_cap"] == 50


def test_row_cap_evicted_rows_are_aggregated_not_lost(repo):
    """Lo que el techo expulsa se agrega antes de borrarse: no se pierden tendencias."""
    now = int(time.time() * 1000)
    base = now - 3_600_000
    repo.insert_samples([_sample(base + i * 1000, accel_x=float(i)) for i in range(100)])

    policy = _policy(repo, max_rows=60)
    policy.run_once()

    assert repo.count_samples() == 60
    # Las 40 expulsadas viven en samples_agg (5-min buckets de 60 s de datos).
    agg = repo._conn().execute(
        "SELECT SUM(n) AS n FROM samples_agg WHERE metric = 'accel_x'"
    ).fetchone()
    assert agg["n"] == 40, "las filas expulsadas por el techo deben quedar agregadas"


def test_row_cap_inactive_when_under_limit(repo):
    now = int(time.time() * 1000)
    repo.insert_samples([_sample(now - 1000 + i) for i in range(10)])
    policy = _policy(repo, max_rows=50)
    summary = policy.run_once()
    assert summary["purged_by_cap"] == 0
    assert repo.count_samples() == 10


# ---------------------------------------------------------------------------
# 3. Agregación histórica
# ---------------------------------------------------------------------------


def test_aggregation_buckets_and_detail_deleted(repo):
    # Dos buckets de 5 min con valores conocidos, en el pasado.
    b0 = ((int(time.time() * 1000) // BUCKET_MS) * BUCKET_MS) - 10 * BUCKET_MS
    b1 = b0 + BUCKET_MS
    repo.insert_samples(
        [
            _sample(b0 + 1_000, accel_x=1.0),
            _sample(b0 + 1_020, accel_x=2.0),
            _sample(b0 + 1_040, accel_x=3.0),
            _sample(b1 + 1_000, accel_x=10.0),
            _sample(b1 + 1_020, accel_x=20.0),
        ]
    )
    # Un ADXL345 sin giroscopio: sus métricas gyro_* no deben aparecer.
    repo.insert_samples(
        [
            _sample(b0 + 1_060, sensor="adxl345", accel_x=5.0),
            _sample(b0 + 1_080, sensor="adxl345", accel_x=7.0),
        ]
    )

    policy = _policy(repo, aggregate_after_days=0)  # todo lo pasado se agrega
    summary = policy.run_once()

    assert summary["errors"] == []
    assert repo.count_samples() == 0, "el detalle agregado debe borrarse"
    assert summary["aggregated_rows"] == 7

    agg = repo._conn().execute(
        "SELECT bucket_ts_ms, sensor, metric, n, avg, min, max FROM samples_agg"
        " WHERE metric = 'accel_x' ORDER BY bucket_ts_ms, sensor"
    ).fetchall()
    got = {(row["bucket_ts_ms"], row["sensor"]): dict(row) for row in agg}

    r = got[(b0, "mpu6050")]
    assert (r["n"], r["avg"], r["min"], r["max"]) == (3, 2.0, 1.0, 3.0)
    r = got[(b1, "mpu6050")]
    assert (r["n"], r["avg"], r["min"], r["max"]) == (2, 15.0, 10.0, 20.0)
    r = got[(b0, "adxl345")]
    assert (r["n"], r["avg"], r["min"], r["max"]) == (2, 6.0, 5.0, 7.0)

    gyro_rows = repo._conn().execute(
        "SELECT COUNT(*) AS n FROM samples_agg"
        " WHERE sensor = 'adxl345' AND metric LIKE 'gyro%'"
    ).fetchone()
    assert gyro_rows["n"] == 0, "un ADXL345 no tiene giroscopio: n=0 no genera fila"


def test_aggregation_merges_late_arrivals(repo):
    """Una llegada tardía a un bucket ya agregado fusiona, no duplica."""
    b0 = ((int(time.time() * 1000) // BUCKET_MS) * BUCKET_MS) - 5 * BUCKET_MS
    repo.insert_samples([_sample(b0 + 1_000, accel_x=1.0), _sample(b0 + 1_020, accel_x=3.0)])
    policy = _policy(repo, aggregate_after_days=0)
    policy.run_once()

    # Llegada tardía dentro del MISMO bucket (p. ej. reinyección de un log).
    repo.insert_samples([_sample(b0 + 1_040, accel_x=5.0)])
    policy.run_once()

    row = repo._conn().execute(
        "SELECT n, avg, min, max FROM samples_agg"
        " WHERE bucket_ts_ms = ? AND sensor = 'mpu6050' AND metric = 'accel_x'",
        (b0,),
    ).fetchone()
    assert (row["n"], row["avg"], row["min"], row["max"]) == (3, 3.0, 1.0, 5.0)


# ---------------------------------------------------------------------------
# 4. La ingesta sigue funcionando mientras corre la purga
# ---------------------------------------------------------------------------


def test_ingest_keeps_working_while_purge_runs(repo):
    now = int(time.time() * 1000)
    # Retrás de 3 días y medio: entra de lleno en la ventana de agregación.
    old_base = now - 3 * DAY_MS - 3_600_000
    backlog = 60_000
    batch = []
    for i in range(backlog):
        batch.append(_sample(old_base + i * 4_000, accel_x=float(i % 10)))
        if len(batch) >= 10_000:
            repo.insert_samples(batch)
            batch = []
    if batch:
        repo.insert_samples(batch)

    policy = _policy(repo, aggregate_after_days=1)  # agrega lo de > 1 día
    errors: list = []
    inserted_during: list = []

    def purge_thread() -> None:
        try:
            policy.run_once()
        except Exception as exc:  # pragma: no cover - fallo del test
            errors.append(exc)

    th = threading.Thread(target=purge_thread, name="purge-sim")
    th.start()
    # "Ingesta" continua mientras la purga corre: mismas primitivas que el
    # hilo de ingesta real (insert_samples sobre el MISMO Repository).
    i = 0
    while th.is_alive():
        try:
            repo.insert_samples([_sample(now + i, accel_x=1.0)])
            inserted_during.append(i)
        except Exception as exc:  # pragma: no cover - fallo del test
            errors.append(exc)
        i += 1
        if i > 5_000:  # salvaguarda para no colgar el test si algo va mal
            break
    th.join(timeout=30.0)
    assert not th.is_alive(), "la pasada de retención no terminó"
    assert errors == [], f"la purga bloqueó o rompió la ingesta: {errors}"
    assert inserted_during, "el test no llegó a insertar durante la purga"

    summary = policy.last_summary
    assert summary["errors"] == []
    assert summary["aggregated_rows"] > 0
    # Las muestras "recientes" insertadas durante la purga siguen ahí.
    assert repo.count_samples() >= len(inserted_during)


# ---------------------------------------------------------------------------
# 5. Tolerancia a fallos
# ---------------------------------------------------------------------------


def test_run_once_never_raises_on_purge_errors(repo, monkeypatch):
    """Un error de purga no debe tumbar nada: se registra y se continúa."""

    def boom(*args, **kwargs):
        raise RuntimeError("disco roto")

    monkeypatch.setattr(repo, "downsample_before", boom)
    monkeypatch.setattr(repo, "purge_older_than", boom)
    monkeypatch.setattr(repo, "count_samples", boom)

    policy = _policy(repo)
    summary = policy.run_once()  # no debe lanzar

    assert summary["errors"], "los fallos deben quedar registrados en el resumen"
    assert policy.last_summary is summary


# ---------------------------------------------------------------------------
# 6. GET /api/db/stats
# ---------------------------------------------------------------------------


class TestDbStatsEndpoint:
    """Endpoint de estado del almacén sobre un servidor uvicorn real."""

    def setup_method(self) -> None:
        import uvicorn

        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmp.name) / "monitoreo.db"

        # Poblado conocido ANTES de arrancar el servidor: 50 muestras
        # recientes + 5 antiguas (a 20 días) para ver ambas en oldest/newest.
        self._now = int(time.time() * 1000)
        self._base = self._now - 3_600_000
        repo = Repository(self._db_path)
        repo.insert_samples([_sample(self._base + i * 1000) for i in range(50)])
        self._old_ts = self._now - 20 * DAY_MS
        repo.insert_samples([_sample(self._old_ts + i) for i in range(5)])
        repo.close_all()

        # simulate=False y un puerto serie inexistente: nada de escritura
        # ajena al test. Política con límites enormes: el arranque no debe
        # tocar el poblado del test.
        from server.app import create_app

        self.app = create_app(
            simulate=False,
            serial_port="/dev/no-existe-para-tests",
            db_path=self._db_path,
            retention_days=3650,
            retention_max_rows=10**9,
            aggregate_after_days=3650,
        )
        uv_config = uvicorn.Config(
            self.app, host="127.0.0.1", port=0, log_level="warning"
        )
        self.server = uvicorn.Server(uv_config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10.0
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("el servidor de prueba no arrancó")
            time.sleep(0.05)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def teardown_method(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10.0)
        self._tmp.cleanup()

    def _get_stats(self) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5.0)
        try:
            conn.request("GET", "/api/db/stats")
            res = conn.getresponse()
            assert res.status == 200
            return json.loads(res.read())
        finally:
            conn.close()

    def test_db_stats_returns_coherent_numbers(self) -> None:
        stats = self._get_stats()

        # Presencia de los campos que necesita el operador.
        for key in (
            "file_bytes",
            "samples_rows",
            "samples_agg_rows",
            "oldest_ts_ms",
            "newest_ts_ms",
            "retention_days",
            "max_rows",
            "aggregate_after_days",
            "est_daily_bytes",
            "next_purge_ts_ms",
        ):
            assert key in stats, f"falta el campo {key}"

        # Coherencia con lo insertado en el test.
        assert stats["samples_rows"] == 55
        assert stats["samples_agg_rows"] == 0
        assert stats["oldest_ts_ms"] == self._old_ts
        assert stats["newest_ts_ms"] == self._base + 49 * 1000
        assert stats["file_bytes"] > 0
        assert stats["est_daily_bytes"] >= 0

        # La política refleja la configuración con la que se arrancó.
        assert stats["retention_days"] == 3650
        assert stats["max_rows"] == 10**9
        assert stats["aggregate_after_days"] == 3650

        # La tarea de fondo está viva: hay próxima pasada programada. Se
        # sondea unos instantes porque el primer tick del bucle puede llegar
        # tras el primer GET (cuestión de milisegundos del scheduler).
        deadline = time.time() + 2.0
        while stats["next_purge_ts_ms"] is None and time.time() < deadline:
            time.sleep(0.05)
            stats = self._get_stats()
        assert isinstance(stats["next_purge_ts_ms"], int)
        assert stats["next_purge_ts_ms"] > self._now - 60_000

    def test_db_stats_does_not_break_existing_routes(self) -> None:
        """Las rutas previas siguen respondiendo con su forma de siempre."""
        self._get_stats()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5.0)
        try:
            conn.request("GET", "/api/history?sensor=mpu6050&seconds=7200&limit=100")
            res = conn.getresponse()
            assert res.status == 200
            body = json.loads(res.read())
        finally:
            conn.close()
        assert set(body) >= {"sensor", "from_ms", "to_ms", "count", "series"}
        assert body["count"] > 0

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5.0)
        try:
            conn.request("GET", "/api/health")
            res = conn.getresponse()
            assert res.status == 200
            assert json.loads(res.read())["status"] == "ok"
        finally:
            conn.close()
