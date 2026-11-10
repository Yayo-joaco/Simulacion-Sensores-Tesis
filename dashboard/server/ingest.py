"""Hilo de ingesta: lee el puerto serie (o el simulador) y alimenta el sistema.

Por qué un hilo aparte y no una corrutina: pyserial es bloqueante y el
contrato (§6) exige que la ingesta a 50 Hz **no bloquee** el hilo HTTP. Un
hilo lector con colas hacia el event loop de asyncio separa ambos mundos sin
código de bajo nivel en el parser.

Responsabilidades:

* abrir el puerto a 115200 baudios y leer líneas UTF-8 terminadas en ``\\n``;
* pasar cada línea por :class:`~server.protocol.ProtocolParser`;
* calcular las métricas derivadas y publicar los mensajes WebSocket;
* acumular muestras y volcarlas a SQLite por lotes (50 filas o 200 ms);
* sobrevivir a todo: puerto inexistente, ESP32 reiniciado, cable
  desconectado… se reintenta cada 2 s y se marca ``connected = false``
  (contrato §2.3), pero el servidor nunca se cae (contrato §6).

Además implementa ``--simulate``: genera tráfico sintético realista
(MPU6050 con gravedad, ruido y deriva de yaw; ADXL345 con vibración senoidal)
formateando **las mismas líneas seriales del contrato** y pasándolas por el
mismo parser. Así la simulación ejercita exactamente el mismo camino que el
hardware y sirve como fuente de datos de prueba documentada en la tesis.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time

from . import config
from .db import Repository
from .derived import DerivedEngine
from .protocol import LogEvent, ProtocolParser, Sample

try:
    import serial  # pyserial
    from serial import SerialException
except Exception:  # pragma: no cover - pyserial es dependencia obligatoria
    serial = None

    class SerialException(Exception):
        """Sustituto para poder importar el módulo sin pyserial en pruebas."""


log = logging.getLogger(__name__)


class EventCollapser:
    """Agrega ráfagas de eventos idénticos en un único aviso ``×N``.

    Por qué: un sensor con el bus I2C degradado repite la misma línea de error
    ~10 veces por segundo durante toda la avería. Sin agregación, la consola de
    eventos se inunda —y la tabla ``events`` se llena de filas redundantes—
    hasta hacer ilegible el diagnóstico que justifica su existencia.

    Comportamiento:

    * La primera aparición de un mensaje se publica **tal cual e
      inmediatamente**: el operador no espera para ver el aviso.
    * Las repeticiones siguientes se cuentan en silencio y se publican como un
      único aviso con el sufijo ``(×N repeticiones)`` como máximo cada
      ``window_s`` segundos, o al cambiar de mensaje (la ráfaga se cierra con
      su conteo antes de publicar el mensaje nuevo).
    * ``N`` son las ocurrencias desde el aviso anterior (la primera aparición
      ya se publicó sin sufijo); el mensaje del aviso agregado es el de la
      última ocurrencia, que lleva las magnitudes más recientes del fallo.

    La clave de agregación es ``(nivel, sensor, code)``: el ``code`` del parser
    es el texto antes del primer ``" - "`` (p. ej. ``MUESTRAS INVALIDAS``), de
    modo que líneas cuyo detalle numérico varía (las magnitudes del
    diagnóstico de descartes) se agrupan igualmente. Sin ``code`` se usa el
    mensaje completo. La lógica es pura (sin E/S) para poder testearla sin
    repositorio ni hilos.
    """

    def __init__(self, window_s: float = config.EVENT_DEDUP_S) -> None:
        self.window_s = window_s
        self._key: tuple | None = None
        self._event: LogEvent | None = None
        self._count = 0
        self._last_pub = 0.0

    @staticmethod
    def _key_of(event: LogEvent) -> tuple:
        return (event.level, event.sensor, event.code or event.message)

    def push(
        self, event: LogEvent, now: float | None = None
    ) -> list[tuple[LogEvent, str]]:
        """Procesa una ocurrencia y devuelve los avisos a publicar.

        Cada aviso es un par ``(evento, sufijo)``; el sufijo va vacío en la
        primera aparición y con ``(×N repeticiones)`` en los avisos agregados.
        """
        now = time.monotonic() if now is None else now
        key = self._key_of(event)
        out: list[tuple[LogEvent, str]] = []

        if key == self._key:
            self._count += 1
            self._event = event  # el agregado lleva la última ocurrencia
            if now - self._last_pub < self.window_s:
                return []  # repetición suprimida
            out.append((event, self._suffix()))
            self._count = 0
            self._last_pub = now
            return out

        # Mensaje distinto: cerrar la ráfaga anterior si acumuló repeticiones.
        if self._count and self._event is not None:
            out.append((self._event, self._suffix()))
            self._count = 0
        self._key = key
        self._event = event
        self._last_pub = now
        out.append((event, ""))
        return out

    def _suffix(self) -> str:
        return f" (×{self._count} repeticiones)" if self._count else ""


class IngestService:
    """Orquesta la fuente de datos y publica hacia base de datos y WebSocket."""

    def __init__(
        self,
        repo: Repository,
        on_message,
        serial_port: str = config.SERIAL_PORT,
        baud: int = config.SERIAL_BAUD,
        simulate: bool = False,
        parser: ProtocolParser | None = None,
    ) -> None:
        self._repo = repo
        # Callback con firma ``fn(dict)``. Puede llamarse desde cualquier
        # hilo: la implementación de app.py reenvía al event loop de forma
        # segura (loop.call_soon_threadsafe).
        self._on_message = on_message
        self._serial_port = serial_port
        self._baud = baud
        self._simulate = simulate
        self._parser = parser or ProtocolParser()

        # Un motor de derivadas por sensor: el estado (ángulos, ventana de
        # vibración, bias del giro) es secuencial y no debe mezclarse entre
        # sensores.
        self._engines = {
            sensor: DerivedEngine(sensor) for sensor in config.KNOWN_SENSORS
        }
        # Agregación de eventos de diagnóstico repetidos (consola legible).
        self._events = EventCollapser()

        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

        # Estado compartido entre el hilo de ingesta y los endpoints HTTP.
        self._lock = threading.Lock()
        self._pending: list[Sample] = []
        self._pending_since_ms = 0
        self._status_delta: dict[str, dict] = {}
        # Los contadores y la última marca de tiempo se restauran de la base
        # de datos: tras un reinicio del servidor el estado que se publica no
        # debe perder el histórico de la sesión anterior.
        stored = repo.get_sensor_status_rows()
        self._n_samples: dict[str, int] = {
            sensor: stored.get(sensor, {}).get("n_samples", 0)
            for sensor in config.KNOWN_SENSORS
        }
        self._last_ts: dict[str, int | None] = {
            sensor: stored.get(sensor, {}).get("last_ts_ms")
            for sensor in config.KNOWN_SENSORS
        }
        self._link_ok = False
        self._last_data_ts_ms = 0
        self._published_status: tuple | None = None

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Arranca el hilo lector (idempotente)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, name="ingest", daemon=True
        )
        self._thread.start()
        log.info(
            "Ingesta iniciada (%s)",
            "simulador" if self._simulate else self._serial_port,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Detiene el hilo y vuelca lo que quede en el buffer."""
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._maybe_flush(force=True)
        log.info("Ingesta detenida")

    # ------------------------------------------------------------------
    # Bucle del hilo lector
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Nunca termina por una excepción: reintenta hasta que se pida parar."""
        while not self._stop_evt.is_set():
            try:
                if self._simulate:
                    self._simulate_loop()
                else:
                    self._serial_loop()
            except Exception:
                # Última red de seguridad: una excepción no contemplada no
                # puede tumbar la ingesta (contrato §6: el servidor sigue vivo).
                log.exception("Error inesperado en el hilo de ingesta")
            self._maybe_flush(force=True)
            if not self._stop_evt.is_set():
                self._set_link(False)
                # Espera interrumpible: el Event permite parar en <100 ms.
                self._stop_evt.wait(config.RECONNECT_DELAY_S)

    def _serial_loop(self) -> None:
        """Abre el puerto, lee líneas y las procesa hasta que falle o se pare."""
        if serial is None:  # pragma: no cover
            raise RuntimeError("pyserial no está instalado")

        try:
            ser = serial.Serial(
                self._serial_port,
                self._baud,
                timeout=config.SERIAL_READ_TIMEOUT_S,
            )
        except (SerialException, OSError, ValueError) as exc:
            # El ESP32 no está enchufado o el puerto está ocupado: no es un
            # error fatal, se reintenta. Caso obligatorio de prueba sin
            # hardware conectado.
            log.warning(
                "No se pudo abrir %s (%s); reintentando en %.1fs",
                self._serial_port,
                exc,
                config.RECONNECT_DELAY_S,
            )
            self._set_link(False)
            self._stop_evt.wait(config.RECONNECT_DELAY_S)
            return

        log.info("Puerto serie abierto: %s @ %d baudios", self._serial_port, self._baud)
        self._set_link(True)
        buffer = b""
        try:
            while not self._stop_evt.is_set():
                try:
                    chunk = ser.read(256)
                except (SerialException, OSError) as exc:
                    # Cable desconectado o ESP32 reiniciado a mitad de lectura.
                    log.warning("Lectura serie fallida: %s", exc)
                    break

                if chunk:
                    buffer += chunk
                    # Se trocea por '\n' conservando el resto: pyserial puede
                    # devolver líneas parciales al agotar el timeout.
                    while b"\n" in buffer:
                        raw_line, buffer = buffer.split(b"\n", 1)
                        self._handle_line(raw_line.decode("utf-8", errors="replace"))
                else:
                    # Sin datos: comprobar la regla de los 2 s del contrato.
                    self._maybe_flush()
                self._maybe_publish_status()
                self._maybe_flush()
        finally:
            try:
                ser.close()
            except Exception:  # pragma: no cover - cierre defensivo
                pass
            self._set_link(False)
            log.info("Puerto serie cerrado: %s", self._serial_port)

    def _simulate_loop(self) -> None:
        """Genera datos sintéticos a 50 Hz sin abrir ningún puerto.

        El bucle usa un reloj monotónico con objetivo por tick para no
        acumular deriva de programación: la cadencia debe ser la del firmware
        real o las métricas de ventana (vib_rms) no serían comparables.
        """
        log.info("Modo simulación: %d Hz, sin puerto serie", config.SIM_TICK_HZ)
        self._set_link(True)
        period = 1.0 / config.SIM_TICK_HZ
        t0 = time.monotonic()
        tick = 0
        rng = random.Random(42)  # semilla fija: pruebas reproducibles
        while not self._stop_evt.is_set():
            target = t0 + tick * period
            delay = target - time.monotonic()
            if delay > 0 and self._stop_evt.wait(delay):
                break
            t = tick * period
            for line in self._simulate_lines(t, rng):
                self._handle_line(line)
            tick += 1
            self._maybe_publish_status()
            self._maybe_flush()
        self._set_link(False)

    # ------------------------------------------------------------------
    # Procesamiento de una línea
    # ------------------------------------------------------------------

    def _handle_line(self, line: str) -> None:
        """Parsea una línea y deriva su flujo: muestra, evento o nada."""
        item = self._parser.parse(line)
        if item is None:
            return
        if isinstance(item, LogEvent):
            self._handle_event(item)
        else:
            self.ingest_sample(item)

    def _handle_event(self, event: LogEvent) -> None:
        """Agrega, guarda y publica un evento de diagnóstico como ``log``.

        Las ráfagas de eventos idénticos se agregan (ver :class:`EventCollapser`):
        se guarda y publica un único aviso con el sufijo ``(×N repeticiones)``
        en lugar de decenas de filas idénticas por segundo.
        """
        for item, suffix in self._events.push(event):
            self._publish_event(item, suffix)

    def _publish_event(self, event: LogEvent, suffix: str) -> None:
        """Guarda en SQLite y publica por WebSocket un evento ya agregado."""
        message = event.message + suffix
        try:
            self._repo.insert_event(
                LogEvent(event.ts_ms, event.sensor, event.level, event.code, message)
            )
        except Exception:
            log.exception("No se pudo guardar el evento: %s", message)
        self._on_message(
            {
                "type": "log",
                "ts_ms": event.ts_ms,
                "level": event.level,
                "sensor": event.sensor,
                "line": message,
            }
        )

    def ingest_sample(self, sample: Sample) -> dict:
        """Punto de entrada único de una muestra (serie, simulador o HTTP).

        Devuelve el mensaje ``sample`` ya construido (formato del contrato
        §4.2) para que ``POST /api/ingest`` pueda hacer eco de las derivadas.
        """
        engine = self._engines.get(sample.sensor)
        derived = engine.update(sample) if engine is not None else {}

        msg = {
            "type": "sample",
            "sensor": sample.sensor,
            "ts_ms": sample.ts_ms,
            "raw": sample.raw_dict(),
            "derived": derived,
        }

        with self._lock:
            self._pending.append(sample)
            if not self._pending_since_ms:
                self._pending_since_ms = sample.ts_ms
            self._n_samples[sample.sensor] = self._n_samples.get(sample.sensor, 0) + 1
            self._last_ts[sample.sensor] = sample.ts_ms
            self._last_data_ts_ms = max(self._last_data_ts_ms, sample.ts_ms)
            delta = self._status_delta.setdefault(
                sample.sensor, {"n_delta": 0, "last_ts_ms": None}
            )
            delta["n_delta"] += 1
            delta["last_ts_ms"] = sample.ts_ms

        # El volcado se intenta en cada muestra: si el lote está lleno o ha
        # pasado el tiempo límite se escribe; si no, es un no-op barato.
        self._maybe_flush()
        self._on_message(msg)
        self._maybe_publish_status()
        return msg

    # ------------------------------------------------------------------
    # Volcado por lotes a SQLite
    # ------------------------------------------------------------------

    def _maybe_flush(self, force: bool = False) -> None:
        """Vuelca el buffer de muestras si toca (50 filas o 200 ms)."""
        now_ms = int(time.time() * 1000)
        with self._lock:
            due = force or len(self._pending) >= config.BATCH_MAX_ROWS or (
                bool(self._pending)
                and (now_ms - self._pending_since_ms) >= config.BATCH_FLUSH_MS
            )
            if not due:
                return
            batch = self._pending
            self._pending = []
            self._pending_since_ms = 0
            deltas = self._status_delta
            self._status_delta = {}

        if not batch and not deltas:
            return
        try:
            if batch:
                self._repo.insert_samples(batch)
            for sensor, delta in deltas.items():
                self._repo.upsert_sensor_status(
                    sensor,
                    present=self._is_present(sensor),
                    last_ts_ms=delta["last_ts_ms"],
                    n_delta=delta["n_delta"],
                )
        except Exception:
            # La base de datos no puede tumbar la ingesta; las muestras de este
            # lote se pierden pero el flujo continúa (se registra el error).
            log.exception("Fallo al escribir el lote de %d muestras", len(batch))

    # ------------------------------------------------------------------
    # Estado y publicación
    # ------------------------------------------------------------------

    @property
    def parse_errors(self) -> int:
        """Líneas con prefijo válido pero mal formadas (contrato §2.3)."""
        return self._parser.parse_errors

    def _is_present(self, sensor: str) -> bool:
        """Un sensor está presente si llegó una muestra válida hace < 2 s."""
        with self._lock:
            last = self._last_ts.get(sensor)
            now_ms = int(time.time() * 1000)
            return last is not None and (now_ms - last) < config.STALE_AFTER_MS

    def status_dict(self) -> dict:
        """Estado rico para ``GET /api/status`` (superset del mensaje WS)."""
        now_ms = int(time.time() * 1000)
        with self._lock:
            last_data = self._last_data_ts_ms
            link_ok = self._link_ok
            sensors = {
                sensor: {
                    "present": self._last_ts[sensor] is not None
                    and (now_ms - self._last_ts[sensor]) < config.STALE_AFTER_MS,
                    "last_ts_ms": self._last_ts[sensor],
                    "n_samples": self._n_samples.get(sensor, 0),
                }
                for sensor in config.KNOWN_SENSORS
            }
        connected = bool(link_ok and last_data and (now_ms - last_data) < config.STALE_AFTER_MS)
        return {
            "connected": connected,
            "port": self._port_label() if link_ok else None,
            "simulate": self._simulate,
            "parse_errors": self.parse_errors,
            "samples_total": sum(s["n_samples"] for s in sensors.values()),
            "sensors": sensors,
        }

    def status_message(self) -> dict:
        """Mensaje ``status`` con **exactamente** la forma del contrato §4.2."""
        info = self.status_dict()
        return {
            "type": "status",
            "connected": info["connected"],
            "port": info["port"],
            "sensors": info["sensors"],
        }

    def _port_label(self) -> str:
        return config.SIM_PORT_LABEL if self._simulate else self._serial_port

    def _set_link(self, ok: bool) -> None:
        """Registra el estado del enlace y publica si el estado cambió."""
        with self._lock:
            changed = self._link_ok != ok
            self._link_ok = ok
        # Solo se fuerza la publicación si el enlace cambió: los reintentos
        # cada 2 s con el puerto caído no deben repetir el mismo "status".
        self._maybe_publish_status(force=changed)

    def _maybe_publish_status(self, force: bool = False) -> None:
        """Publica ``status`` solo cuando el estado cambia (contrato §4.2).

        El contrato dice "cada vez que cambia el estado de un sensor": eso es
        connected/port/present. Los contadores ``last_ts_ms`` y ``n_samples``
        avanzan con cada muestra y **no** cuentan como cambio de estado; si se
        incluyeran en la comparación se emitiría un status a 50 Hz por sensor
        ahogando al dashboard.
        """
        message = self.status_message()
        snapshot = (
            message["connected"],
            message["port"],
            tuple(
                (s, v["present"])
                for s, v in message["sensors"].items()
            ),
        )
        with self._lock:
            if not force and snapshot == self._published_status:
                return
            self._published_status = snapshot
        self._on_message(message)

    # ------------------------------------------------------------------
    # Simulador de datos
    # ------------------------------------------------------------------

    def _simulate_lines(self, t: float, rng: random.Random) -> list[str]:
        """Genera las líneas seriales sintéticas del instante ``t`` (segundos).

        Modelo físico (documentado en la tesis como fuente de datos de prueba):

        * **MPU6050**: gravedad + balanceo lento de la torre (±2° roll a
          0,10 Hz, ±1,5° pitch a 0,07 Hz) + ruido blanco. El giroscopio
          devuelve la derivada de ese movimiento más su bias. El bias del eje
          Z varía lentamente (inestabilidad real del MEMS) para que el yaw
          **derive** incluso con calibración, tal como advierte el contrato.
        * **ADXL345**: gravedad + vibración senoidal de 5 Hz y 12 Hz (las
          frecuencias típicas de una estructura metálica agitada por el
          viento) + ruido.
        * **Temperatura**: deriva lenta alrededor de 26,3 °C.

        El formateo (%.3f aceleración, %.5f giro, %.2f temperatura) es el del
        contrato §2.1, de modo que el parser recibe exactamente lo que
        produciría el firmware.
        """
        g = config.GRAVITY

        # Ángulos de referencia (grados) y sus derivadas (grados/s).
        roll_deg = 2.0 * math.sin(2.0 * math.pi * 0.10 * t)
        pitch_deg = 1.5 * math.sin(2.0 * math.pi * 0.07 * t + 1.0)
        roll_rate = 2.0 * (2.0 * math.pi * 0.10) * math.cos(2.0 * math.pi * 0.10 * t)
        pitch_rate = 1.5 * (2.0 * math.pi * 0.07) * math.cos(
            2.0 * math.pi * 0.07 * t + 1.0
        )

        roll_rad = math.radians(roll_deg)
        pitch_rad = math.radians(pitch_deg)

        # Aceleración específica en el marco del sensor (gravedad rotada).
        ax = -g * math.sin(pitch_rad) + rng.gauss(0.0, 0.02)
        ay = g * math.sin(roll_rad) + rng.gauss(0.0, 0.02)
        az = g * math.cos(roll_rad) * math.cos(pitch_rad) + rng.gauss(0.0, 0.02)

        # Bias del giroscopio: constante de fabricación + inestabilidad lenta
        # (esta última es la que produce deriva de yaw tras calibrar).
        bias_x = 0.0015 + 0.0004 * math.sin(2.0 * math.pi * t / 240.0)
        bias_y = -0.0012 + 0.0004 * math.sin(2.0 * math.pi * t / 180.0)
        bias_z = 0.0030 + 0.0020 * math.sin(2.0 * math.pi * t / 300.0)

        gx = math.radians(roll_rate) + bias_x + rng.gauss(0.0, 0.003)
        gy = math.radians(pitch_rate) + bias_y + rng.gauss(0.0, 0.003)
        gz = bias_z + rng.gauss(0.0, 0.003)  # sin rotación real: deriva pura

        temp = 26.3 + 0.25 * math.sin(2.0 * math.pi * t / 120.0) + rng.gauss(0.0, 0.02)

        mpu_line = (
            f"DATA,{ax:.3f},{ay:.3f},{az:.3f},"
            f"{gx:.5f},{gy:.5f},{gz:.5f},{temp:.2f}"
        )

        # Vibración estructural: dos armónicos + ruido, sobre la gravedad.
        vib = 0.45 * math.sin(2.0 * math.pi * 5.0 * t) + 0.18 * math.sin(
            2.0 * math.pi * 12.0 * t + 0.7
        )
        dax = 0.6 * vib + rng.gauss(0.0, 0.03)
        day = 0.3 * vib + rng.gauss(0.0, 0.03)
        daz = g + vib + rng.gauss(0.0, 0.03)

        adxl_line = f"ADXL,{dax:.3f},{day:.3f},{daz:.3f}"

        return [mpu_line, adxl_line]
