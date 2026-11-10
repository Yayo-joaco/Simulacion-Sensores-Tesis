"""Métricas derivadas del contrato de datos (``CONTRATO_DE_DATOS.md`` §5).

Por qué una capa única de derivadas: la visualización en vivo, el histórico y
la futura detección de anomalías deben compartir exactamente los mismos
valores. Si cada consumidor calculase su propio roll o su propio vib_rms, los
umbrales del capítulo de detección no serían reproducibles.

Definiciones del contrato §5 (inamovibles):

==================  =========================================================
``accel_mag``       √(ax²+ay²+az²) en m/s²
``gyro_mag``        √(gx²+gy²+gz²) en rad/s
``roll``            atan2(ay, az) en grados, filtrado complementario α = 0.96
``pitch``           atan2(-ax, √(ay²+az²)) en grados, ídem
``yaw``             integración de gyro_z con bias corregido, envuelto a
                    (-180, 180]
``vib_rms``         RMS de |√(ax²+ay²+az²) - g| en ventana de 1 s
``vib_peak``        máximo de esa magnitud en la ventana de 1 s
==================  =========================================================

El filtro complementario mezcla la inclinación del acelerómetro (que no
deriva pero es ruidosa en movimiento) con la integración del giroscopio (que
es suave pero deriva) con α = 0.96, exactamente como el visualizador 3D en
Processing (ver ``../processing/README.md``) para que ambos visores coincidan.

Estado secuencial: cada sensor tiene su propio motor (:class:`DerivedEngine`)
porque roll/pitch/yaw y la ventana de vibración dependen del historial. El
motor es **monotarea**: lo usa el hilo de ingesta o la petición HTTP que
reescribe el histórico, nunca ambos a la vez sin un cerrojo.
"""

from __future__ import annotations

import math
from collections import deque

from . import config
from .protocol import Sample


def wrap_deg180(angle: float) -> float:
    """Envuelve un ángulo al rango (-180, 180] como exige el contrato §5.

    El matiz del intervalo abierto a la izquierda importa: -180 y 180 son el
    mismo rumbo, pero el contrato fija que el valor representado sea +180.
    """
    y = math.fmod(angle + 180.0, 360.0)
    if y <= 0.0:
        y += 360.0
    return y - 180.0


class DerivedEngine:
    """Calcula las métricas derivadas de un sensor, muestra a muestra.

    Parámetros tomados del contrato y del visualizer:

    * ``alpha`` = 0.96 (peso del giroscopio en roll/pitch).
    * ``gravity`` = 9.80665 m/s².
    * ``bias_samples`` = 50 (~1 s a 50 Hz con la placa en reposo): es la misma
      calibración que muestra el visualizer ("CALIBRANDO GIROSCOPIO").
    * ``max_gap_s`` = 0.5: si llega una muestra tras un hueco mayor, el paso
      no se integra, para que el hueco no se interprete como una rotación
      enorme (misma regla que el visualizer reiniciando su filtro).
    """

    def __init__(
        self,
        sensor: str,
        alpha: float = config.COMPLEMENTARY_ALPHA,
        gravity: float = config.GRAVITY,
        window_ms: int = config.VIB_WINDOW_MS,
        bias_samples: int = config.GYRO_BIAS_SAMPLES,
        max_gap_s: float = config.FILTER_MAX_GAP_S,
    ) -> None:
        self.sensor = sensor
        self.alpha = alpha
        self.gravity = gravity
        self.window_ms = window_ms
        self.bias_samples = bias_samples
        self.max_gap_s = max_gap_s

        # Ángulos de estado en grados (salida del contrato).
        self._roll = 0.0
        self._pitch = 0.0
        self._yaw = 0.0
        self._last_ts_ms: int | None = None
        self._initialized = False

        # Calibración del bias del giroscopio (rad/s).
        self._bias_buf: list[tuple[float, float, float]] = []
        self._bias = (0.0, 0.0, 0.0)

        # Ventana deslizante de vibración: pares (ts_ms, magnitud).
        self._vib: deque[tuple[int, float]] = deque()

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def update(self, sample: Sample) -> dict:
        """Procesa una muestra y devuelve las métricas derivadas del contrato.

        El diccionario solo incluye las métricas que corresponden al sensor
        (contrato §4.2): roll/pitch/yaw y gyro_mag solo para MPU6050,
        vib_rms/vib_peak para MPU6050 y ADXL345.
        """
        out: dict[str, float] = {}

        accel_mag = math.sqrt(
            sample.accel_x**2 + sample.accel_y**2 + sample.accel_z**2
        )
        # Precios de redondeo alineados con el formato seriales del contrato
        # §2.1 (%.3f aceleración, %.5f giro) y %.2f para ángulos de pantalla:
        # el JSON por WebSocket va a 100 msg/s y no aporta nada cifrar ruido
        # de coma flotante por debajo de la resolución física del sensor.
        out["accel_mag"] = round(accel_mag, 3)

        gap = self._track_time(sample.ts_ms)

        if sample.has_gyro:
            gx, gy, gz = sample.gyro_x or 0.0, sample.gyro_y or 0.0, sample.gyro_z or 0.0
            out["gyro_mag"] = round(math.sqrt(gx**2 + gy**2 + gz**2), 5)

            self._update_gyro_bias(gx, gy, gz)
            bx, by, bz = self._bias
            gx_c, gy_c, gz_c = gx - bx, gy - by, gz - bz

            # Inclinación que da solo el acelerómetro (en grados).
            roll_acc = math.degrees(math.atan2(sample.accel_y, sample.accel_z))
            pitch_acc = math.degrees(
                math.atan2(
                    -sample.accel_x,
                    math.sqrt(sample.accel_y**2 + sample.accel_z**2),
                )
            )

            if not self._initialized or gap:
                # Primer dato o dato tras un hueco: el filtro se arranca desde
                # la inclinación del acelerómetro y no se integra el paso, de
                # forma que un hueco no se convierte en una rotación falsa.
                self._roll = roll_acc
                self._pitch = pitch_acc
            else:
                dt = self._dt_s
                # Filtro complementario (α = 0.96): giroscopio integra,
                # acelerómetro corrige la deriva a largo plazo.
                self._roll = self.alpha * (self._roll + math.degrees(gx_c) * dt) + (
                    1.0 - self.alpha
                ) * roll_acc
                self._pitch = self.alpha * (self._pitch + math.degrees(gy_c) * dt) + (
                    1.0 - self.alpha
                ) * pitch_acc

            if self._initialized and not gap:
                # Yaw solo del giroscopio (sin magnetómetro no hay referencia
                # absoluta): deriva con el tiempo, como advierte el contrato.
                self._yaw = wrap_deg180(self._yaw + math.degrees(gz_c) * self._dt_s)

            self._initialized = True

            out["roll"] = round(self._roll, 2)
            out["pitch"] = round(self._pitch, 2)
            out["yaw"] = round(self._yaw, 2)

        # Vibración: magnitud de la aceleración con la gravedad retirada.
        vib = abs(accel_mag - self.gravity)
        self._vib.append((sample.ts_ms, vib))
        cutoff = sample.ts_ms - self.window_ms
        while self._vib and self._vib[0][0] < cutoff:
            self._vib.popleft()

        values = [v for _, v in self._vib]
        vib_rms = math.sqrt(sum(v * v for v in values) / len(values))
        out["vib_rms"] = round(vib_rms, 3)
        out["vib_peak"] = round(max(values), 3)

        return out

    def reset(self) -> None:
        """Reinicia el estado (usado al reconectar o en pruebas)."""
        self._roll = 0.0
        self._pitch = 0.0
        self._yaw = 0.0
        self._last_ts_ms = None
        self._initialized = False
        self._bias_buf.clear()
        self._bias = (0.0, 0.0, 0.0)
        self._vib.clear()

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _track_time(self, ts_ms: int) -> bool:
        """Actualiza el reloj del filtro y devuelve si hubo hueco temporal.

        El dt real (diferencia de marcas de tiempo) se usa para integrar: a
        50 Hz nominal son 20 ms, pero el bucle del ESP32 no es perfecto y un
        dt fijo acumularía error sistemático en yaw.
        """
        gap = False
        if self._last_ts_ms is None:
            self._dt_s = 1.0 / config.SIM_TICK_HZ
        else:
            dt_s = (ts_ms - self._last_ts_ms) / 1000.0
            if dt_s <= 0.0:
                # Marca de tiempo igual o retrocedida (reinicio del ESP32):
                # se ignora el paso para no integrar hacia atrás.
                dt_s = 1.0 / config.SIM_TICK_HZ
                gap = True
            elif dt_s > self.max_gap_s:
                gap = True
            # Se limita dt a un valor razonable para que un salto de reloj no
            # produzca una integración gigante aunque no se considere hueco.
            self._dt_s = min(dt_s, 0.25)
        self._last_ts_ms = ts_ms
        return gap

    def _update_gyro_bias(self, gx: float, gy: float, gz: float) -> None:
        """Media acumulada de los primeros N giros con la placa en reposo.

        El contrato remite a ../processing/README.md: se pide al operador
        mantener la placa quieta ~1 s (50 muestras). Hasta completar esas
        muestras el bias es la media parcial, de modo que la calibración no
        produce un salto en yaw.
        """
        if len(self._bias_buf) >= self.bias_samples:
            return
        self._bias_buf.append((gx, gy, gz))
        n = len(self._bias_buf)
        self._bias = (
            sum(v[0] for v in self._bias_buf) / n,
            sum(v[1] for v in self._bias_buf) / n,
            sum(v[2] for v in self._bias_buf) / n,
        )
