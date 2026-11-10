"""Pruebas de regresión de las rutas de lectura ante descargas CSV abortadas.

Por qué estas pruebas existen: se detectó que un ``GET /api/export.csv``
abortado por el cliente (``curl | head``, cancelación en el navegador) dejaba
el SELECT del streaming sin finalizar sobre la conexión thread-local del hilo
del threadpool que sirvió la descarga. Ese snapshot WAL clavado hacía que
``/api/history`` sirviera datos congelados durante minutos en ese hilo, sin
error en los logs. La corrección (Repository.iter_csv con conexión dedicada y
``try/finally``, más un BackgroundTask que cierra el generador) no debe volver
a permitirlo.

Dos niveles de prueba:

* :func:`test_abandoned_export_does_not_freeze_history` — nivel repositorio,
  determinista: reproduce exactamente el mecanismo (mismo hilo, misma conexión
  thread-local, generador abandonado a media descarga) y exige que el
  histórico posterior siga viendo datos frescos.
* :func:`test_history_stays_fresh_after_csv_abort` — extremo a extremo con un
  servidor uvicorn real y simulador a 50 Hz: aborta la descarga por socket y
  comprueba que ``/api/history`` sigue devolviendo datos frescos.

Se ejecutan con la instalación del proyecto (sin pytest ni httpx):

    .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import http.client
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

# El test se ejecuta desde tests/; el paquete `server` es hermano de este
# directorio dentro de dashboard/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.app import create_app  # noqa: E402
from server.db import Repository  # noqa: E402
from server.protocol import Sample  # noqa: E402

# Ventana de frescura: el simulador escribe a 50 Hz y los lotes se vuelcan
# cada 200 ms, así que cualquier histórico sano trae datos de hace < 2 s.
FRESH_MS = 3000


def _sample(ts_ms: int, sensor: str = "mpu6050") -> Sample:
    return Sample(
        ts_ms=ts_ms,
        sensor=sensor,
        accel_x=0.1,
        accel_y=-0.2,
        accel_z=9.8,
        gyro_x=0.001,
        gyro_y=0.002,
        gyro_z=0.003,
        temp_c=25.5,
    )


class ExportAbortRepositoryTest(unittest.TestCase):
    """El abort de una exportación no debe congelar el histórico del hilo."""

    def test_abandoned_export_does_not_freeze_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "monitoreo.db"
            repo_reader = Repository(db_path)  # solo lo usa el hilo lector
            repo_writer = Repository(db_path)  # simula el hilo de ingesta

            base = int(time.time() * 1000) - 60_000
            repo_writer.insert_samples([_sample(base + i * 20) for i in range(100)])

            abandoned: list = []
            batch2_written = threading.Event()
            result: dict = {}
            errors: list = []

            def reader_thread() -> None:
                """Lo que hace un hilo del threadpool: exportar y luego leer."""
                try:
                    # Descarga que el cliente abandona a mitad de stream: el
                    # generador queda suspendido en un yield, con el cursor
                    # SELECT vivo. Se guarda la referencia para que el GC no
                    # lo cierre (era justo lo que pasaba con un cliente ido).
                    gen = repo_reader.iter_csv(chunk=10)
                    next(gen)  # cabecera CSV
                    next(gen)  # primer lote de filas
                    next(gen)  # segundo lote: abortada aquí
                    abandoned.append(gen)

                    batch2_written.wait(timeout=5.0)
                    result.update(
                        repo_reader.query_history(
                            sensor="mpu6050",
                            metrics=None,
                            from_ms=base - 1000,
                            to_ms=base + 100_000,
                            limit=500,
                        )
                    )
                except Exception as exc:  # pragma: no cover - fallo del test
                    errors.append(exc)

            th = threading.Thread(target=reader_thread, name="reader-sim")
            th.start()
            time.sleep(0.2)  # deja el generador suspendido a media descarga

            # Llegan muestras NUEVAS mientras la descarga abortada sigue viva.
            newest = base + 50_000
            repo_writer.insert_samples(
                [_sample(newest + i * 20) for i in range(50)]
            )
            batch2_written.set()
            th.join(timeout=5.0)

            self.assertEqual(errors, [])
            self.assertTrue(abandoned, "el generador debía quedar suspendido")
            count = result.get("count", 0)
            self.assertGreater(count, 0, "el histórico quedó congelado en vacío")
            accel_x = result.get("series", {}).get("accel_x", [])
            self.assertTrue(accel_x, "series.accel_x vacía tras el abort")
            max_ts = max(point[0] for point in accel_x)
            self.assertGreaterEqual(
                max_ts,
                newest,
                "el histórico no ve las muestras escritas tras el abort "
                "(snapshot congelado)",
            )
            # El generador abandonado sigue vivo hasta aquí: sus recursos se
            # liberan al cerrarlo, y eso no debe afectar a nuevas lecturas.
            abandoned[0].close()
            again = repo_reader.query_history(
                sensor="mpu6050",
                metrics=None,
                from_ms=base - 1000,
                to_ms=base + 100_000,
                limit=500,
            )
            self.assertGreater(again.get("count", 0), 0)
            repo_reader.close_all()
            repo_writer.close_all()

    def test_default_metrics_include_derived(self) -> None:
        """metrics por defecto = 'todos' (§4.1): raw + derivadas del sensor."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / "monitoreo.db")
            base = int(time.time() * 1000) - 5_000
            repo.insert_samples(
                [_sample(base + i * 20, sensor="adxl345") for i in range(60)]
            )
            result = repo.query_history(
                sensor="adxl345",
                metrics=None,
                from_ms=base - 1000,
                to_ms=base + 60_000,
                limit=500,
            )
            series = result.get("series", {})
            for name in ("accel_x", "accel_y", "accel_z", "vib_rms", "vib_peak"):
                self.assertIn(name, series, f"falta la métrica {name}")
                self.assertTrue(series[name], f"serie {name} vacía")
            repo.close_all()


class ExportAbortHttpTest(unittest.TestCase):
    """Con un servidor real: abortar export.csv no congela /api/history."""

    def setUp(self) -> None:
        import uvicorn

        self._tmp = tempfile.TemporaryDirectory()
        # --simulate: fuente de datos a 50 Hz sin tocar el puerto serie real.
        self.app = create_app(
            simulate=True, db_path=Path(self._tmp.name) / "monitoreo.db"
        )
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=0, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10.0
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("el servidor de prueba no arrancó")
            time.sleep(0.05)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def tearDown(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10.0)
        self._tmp.cleanup()

    def _abort_csv_download(self) -> None:
        """Descarga CSV abortada a nivel de socket: se lee un poco y se cierra."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5.0)
        try:
            sock.sendall(
                b"GET /api/export.csv HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Connection: close\r\n\r\n"
            )
            sock.recv(4096)  # cabecera + primeros bytes del CSV
        finally:
            sock.close()  # el cliente desaparece a mitad de stream

    def _history_probe(self) -> tuple[int, int]:
        """Devuelve (count, ts_max) de /api/history?sensor=mpu6050."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5.0)
        try:
            conn.request("GET", "/api/history?sensor=mpu6050&seconds=30&limit=200")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            import json

            body = json.loads(res.read())
        finally:
            conn.close()
        series = body.get("series", {})
        ts_max = 0
        for points in series.values():
            for point in points:
                ts_max = max(ts_max, point[0])
        return int(body.get("count", 0)), ts_max

    def test_history_stays_fresh_after_csv_abort(self) -> None:
        now_ms = lambda: int(time.time() * 1000)  # noqa: E731

        # Deja que el simulador llene algo de histórico.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            count, _ = self._history_probe()
            if count > 0:
                break
            time.sleep(0.2)

        for _ in range(3):  # tres aborts: el fallo original era intermitente
            self._abort_csv_download()
            # Varios probes tras cada abort: el threadpool reutiliza hilos y
            # el fallo solo se ve en el hilo con la conexión congelada.
            for _ in range(12):
                count, ts_max = self._history_probe()
                self.assertGreater(count, 0, "history vacío tras abortar export.csv")
                age = now_ms() - ts_max
                self.assertLess(
                    age,
                    FRESH_MS,
                    f"history congelado: el dato más reciente tiene {age} ms",
                )
                time.sleep(0.05)


if __name__ == "__main__":
    unittest.main(verbosity=2)
