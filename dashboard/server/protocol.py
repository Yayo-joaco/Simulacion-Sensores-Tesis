"""Parser del protocolo serie definido en ``CONTRATO_DE_DATOS.md`` §2.

Por qué un parser separado del lector serie: el mismo código debe validar las
líneas que llegan por USB, las del simulador y cualquier línea reinyectada en
pruebas. Si la validación viviera dentro del hilo de ingesta no sería posible
probarla sin hardware.

Reglas del contrato que se implementan aquí:

* ``DATA,ax,ay,az,gx,gy,gz,temp`` -> muestra MPU6050 (m/s², rad/s, °C).
* ``ADXL,ax,ay,az``               -> muestra ADXL345 (m/s²).
* Cualquier línea que empiece con ``[`` es texto legible -> evento de log.
* El resto de líneas (restos del bootloader a 74880 baudios, banner de
  arranque, texto corrupto) se **ignoran** sin más: no son error de parseo.
* Una línea con el prefijo correcto pero mal formada (campos de más o de
  menos, o algún valor no finito) se descarta completa y suma ``parse_errors``
  (contrato §2.3): nunca interrumpe la ingesta.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field

from . import config

# Prefijos del contrato §2.1 / §2.2. Son literales del protocolo: no tocar
# sin actualizar el contrato primero.
DATA_PREFIX = "DATA,"
ADXL_PREFIX = "ADXL,"
EVENT_PREFIX = "["


@dataclass(slots=True)
class Sample:
    """Una muestra de telemetría ya validada.

    ``gyro_*`` se mantiene en **rad/s** y ``temp_c`` en °C, tal como estipula
    el contrato §3 para la base de datos; la conversión a grados solo ocurre
    al presentar (métricas derivadas y API).
    """

    ts_ms: int
    sensor: str
    accel_x: float
    accel_y: float
    accel_z: float
    gyro_x: float | None = None
    gyro_y: float | None = None
    gyro_z: float | None = None
    temp_c: float | None = None
    # JSON crudo para auditoría (contrato §3, columna payload): conserva la
    # línea original además de los campos numéricos, de modo que un error de
    # parseo futuro se pueda reproducir sin el log del puerto serie.
    payload: str = field(default="", repr=False)

    @property
    def has_gyro(self) -> bool:
        """True si la muestra incluye giroscopio (solo MPU6050)."""
        return self.gyro_x is not None

    def raw_dict(self) -> dict:
        """Campos ``raw`` del mensaje WebSocket (contrato §4.2).

        Solo incluye los campos que el sensor realmente produce: el ejemplo
        del contrato para ADXL345 no debe arrastrar claves de giroscopio.
        """
        raw = {
            "accel_x": self.accel_x,
            "accel_y": self.accel_y,
            "accel_z": self.accel_z,
        }
        if self.has_gyro:
            raw.update(
                {
                    "gyro_x": self.gyro_x,
                    "gyro_y": self.gyro_y,
                    "gyro_z": self.gyro_z,
                    "temp_c": self.temp_c,
                }
            )
        return raw

    def as_row(self) -> tuple:
        """Tupla con el orden exacto de columnas de la tabla ``samples``."""
        return (
            self.ts_ms,
            self.sensor,
            self.accel_x,
            self.accel_y,
            self.accel_z,
            self.gyro_x,
            self.gyro_y,
            self.gyro_z,
            self.temp_c,
            self.payload,
        )


@dataclass(slots=True)
class LogEvent:
    """Línea de diagnóstico humano (prefijo ``[``), contrato §2.2."""

    ts_ms: int
    sensor: str | None
    level: str  # 'info' | 'warn' | 'error'
    code: str | None
    message: str  # la línea completa, tal cual se leyó

    def as_row(self) -> tuple:
        """Tupla con el orden exacto de columnas de la tabla ``events``."""
        return (self.ts_ms, self.sensor, self.level, self.code, self.message)


def _classify_level(text: str) -> str:
    """Deriva el nivel del evento mirando palabras del propio texto.

    El contrato (§4.2) exige level info|warn|error pero el firmware no manda
    un campo de nivel: se infiere del contenido, que es lo único fiable.
    """
    upper = text.upper()
    if "ERROR" in upper or "FAIL" in upper:
        return "error"
    # "MUESTRAS INVALIDAS" sin más es una advertencia (el firmware descarta
    # datos corruptos pero sigue vivo): debe verse en ámbar, no como info.
    if "WARN" in upper or "INVALID" in upper:
        return "warn"
    return "info"


class ProtocolParser:
    """Convierte líneas de texto en :class:`Sample` / :class:`LogEvent`.

    Mantiene el contador ``parse_errors`` que exige el contrato §2.3. El parser
    es sin estado (no acumula nada entre líneas), así que puede compartirse
    entre el hilo de ingesta y las pruebas sin cerrojos.
    """

    def __init__(self) -> None:
        self.parse_errors = 0

    def parse(self, line: str, ts_ms: int | None = None) -> Sample | LogEvent | None:
        """Procesa una línea y devuelve una muestra, un evento o ``None``.

        ``None`` significa "línea ignorada" (basura del bootloader, vacío,
        eco del monitor serie…), que es distinto de "línea mal formada"
        (prefijo válido pero contenido inválido), que sí cuenta como error.
        """
        text = line.strip()
        if not text:
            return None

        # Marca de tiempo de recepción en epoch UTC ms (contrato §3). Si el
        # llamador ya la tiene (simulador, reinyección) se respeta.
        ts = int(time.time() * 1000) if ts_ms is None else int(ts_ms)

        if text.startswith(DATA_PREFIX):
            return self._parse_mpu6050(text, ts)
        if text.startswith(ADXL_PREFIX):
            return self._parse_adxl345(text, ts)
        if text.startswith(EVENT_PREFIX):
            return self._parse_event(text, ts)

        # Prefijo desconocido: basura del bootloader o banner de arranque.
        # Se ignora sin penalizar (contrato §2.3).
        return None

    # -- parsers por tipo ---------------------------------------------------

    def _parse_mpu6050(self, text: str, ts_ms: int) -> Sample | None:
        fields = text.split(",")
        # "DATA" + 7 valores = 8 tokens exactos. Más o menos es línea corrupta.
        if len(fields) != 1 + len(config.DATA_FIELDS):
            self.parse_errors += 1
            return None
        values = self._to_finite_floats(fields[1:])
        if values is None:
            self.parse_errors += 1
            return None
        ax, ay, az, gx, gy, gz, temp = values
        sample = Sample(
            ts_ms=ts_ms,
            sensor=config.SENSOR_MPU6050,
            accel_x=ax,
            accel_y=ay,
            accel_z=az,
            gyro_x=gx,
            gyro_y=gy,
            gyro_z=gz,
            temp_c=temp,
        )
        sample.payload = self._build_payload(text, sample.raw_dict())
        return sample

    def _parse_adxl345(self, text: str, ts_ms: int) -> Sample | None:
        fields = text.split(",")
        if len(fields) != 1 + len(config.ADXL_FIELDS):
            self.parse_errors += 1
            return None
        values = self._to_finite_floats(fields[1:])
        if values is None:
            self.parse_errors += 1
            return None
        ax, ay, az = values
        sample = Sample(
            ts_ms=ts_ms,
            sensor=config.SENSOR_ADXL345,
            accel_x=ax,
            accel_y=ay,
            accel_z=az,
        )
        sample.payload = self._build_payload(text, sample.raw_dict())
        return sample

    def _parse_event(self, text: str, ts_ms: int) -> LogEvent:
        """Extrae sensor, código y nivel de una línea ``[TAG] ...``.

        Ejemplo: ``[MPU6050] I2C ERROR - sin respuesta en el bus``
        -> sensor ``mpu6050``, level ``error``, code ``I2C ERROR``.
        El código es el texto anterior al primer " - ", que es como el
        firmware etiqueta sus diagnósticos; si no hay separador, code=None.
        """
        sensor: str | None = None
        body = text
        if text.startswith("[") and "]" in text:
            tag, _, rest = text[1:].partition("]")
            tag = tag.strip()
            if tag:
                # Minúsculas: el contrato usa "mpu6050" en el campo sensor.
                sensor = tag.lower()
            body = rest.strip()

        code: str | None = None
        if " - " in body:
            code = body.split(" - ", 1)[0].strip() or None

        return LogEvent(
            ts_ms=ts_ms,
            sensor=sensor,
            level=_classify_level(text),
            code=code,
            message=text,
        )

    # -- utilidades ---------------------------------------------------------

    @staticmethod
    def _to_finite_floats(tokens: list[str]) -> tuple[float, ...] | None:
        """Convierte tokens a float o devuelve ``None`` si alguno no vale.

        El contrato §2.1 es inequívoco: si un valor no es un número finito
        (NaN, inf, texto) **toda la línea se descarta**. Descartar la línea
        completa evita muestras a medias que romperían las métricas derivadas.
        """
        out: list[float] = []
        for token in tokens:
            try:
                value = float(token)
            except ValueError:
                return None
            if not math.isfinite(value):
                return None
            out.append(value)
        return tuple(out)

    @staticmethod
    def _build_payload(line: str, raw: dict) -> str:
        """JSON crudo de auditoría para la columna ``samples.payload``."""
        return json.dumps({"line": line, "raw": raw}, ensure_ascii=False)
