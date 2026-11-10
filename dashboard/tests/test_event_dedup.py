"""Pruebas de la agregación de eventos repetidos (EventCollapser) y del nivel.

Por qué existen: un bus I2C degradado emite la misma línea de error ~10 veces
por segundo durante toda la avería. Sin agregación la consola de eventos del
dashboard se inunda hasta hacer inútil el diagnóstico que justifica su
existencia (y la tabla ``events`` se llena de filas redundantes). Estas
pruebas fijan el contrato del colapso: el primer aviso sale inmediatamente,
las repeticiones se agregan como ``(×N repeticiones)`` sin perder conteo, y
las líneas de muestras inválidas se clasifican como advertencia (ámbar), no
como info.

Se ejecutan con pytest desde dashboard/:

    .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# El test se ejecuta desde tests/; el paquete `server` es hermano de este
# directorio dentro de dashboard/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.ingest import EventCollapser, IngestService  # noqa: E402
from server.protocol import LogEvent, ProtocolParser  # noqa: E402


def _event(
    message: str = "[MPU6050] MUESTRAS INVALIDAS - revise cableado I2C",
    sensor: str | None = "mpu6050",
    level: str = "warn",
    code: str | None = "MUESTRAS INVALIDAS",
    ts_ms: int = 1_000,
) -> LogEvent:
    return LogEvent(ts_ms=ts_ms, sensor=sensor, level=level, code=code, message=message)


class TestEventCollapser:
    def test_primera_aparicion_se_publica_inmediata_y_sin_sufijo(self):
        c = EventCollapser(window_s=30.0)
        out = c.push(_event(), now=0.0)
        assert out == [(out[0][0], "")]

    def test_repeticiones_rapidas_se_suprimen(self):
        c = EventCollapser(window_s=30.0)
        c.push(_event(), now=0.0)
        for i in range(1, 51):
            assert c.push(_event(ts_ms=1_000 + i), now=i * 0.1) == []

    def test_tras_la_ventana_se_publica_un_unico_agregado(self):
        c = EventCollapser(window_s=30.0)
        c.push(_event(), now=0.0)
        for i in range(1, 51):  # 50 repeticiones dentro de la ventana
            c.push(_event(ts_ms=1_000 + i), now=i * 0.1)
        out = c.push(_event(ts_ms=2_000), now=31.0)
        # Un solo aviso que da cuenta de las 51 ocurrencias desde el primero.
        assert len(out) == 1
        item, suffix = out[0]
        assert suffix == " (×51 repeticiones)"
        assert item.message.endswith("revise cableado I2C")

    def test_cambio_de_mensaje_cierra_la_rafaga_con_su_conteo(self):
        c = EventCollapser(window_s=30.0)
        c.push(_event(), now=0.0)
        for i in range(1, 4):
            c.push(_event(ts_ms=1_000 + i), now=i * 0.1)
        otro = _event(
            message="[MPU6050] I2C ERROR - sin respuesta en el bus",
            code="I2C ERROR",
            ts_ms=2_000,
        )
        out = c.push(otro, now=1.0)
        assert len(out) == 2
        # Primero el cierre de la ráfaga anterior (3 repeticiones suprimidas),
        # después la primera aparición del mensaje nuevo, sin sufijo.
        assert out[0][0].message.startswith("[MPU6050] MUESTRAS INVALIDAS")
        assert out[0][1] == " (×3 repeticiones)"
        assert out[1] == (otro, "")

    def test_mismo_code_con_numeros_variables_se_agrupa(self):
        # El diagnóstico de descartes del firmware lleva magnitudes que cambian
        # en cada línea; la clave es el code ("MUESTRAS INVALIDAS"), no el texto.
        c = EventCollapser(window_s=30.0)
        c.push(
            _event(message="[MPU6050] MUESTRAS INVALIDAS - revise cableado I2C (|a|=0.000 m/s2 |w|=0.00000 rad/s)"),
            now=0.0,
        )
        assert c.push(
            _event(message="[MPU6050] MUESTRAS INVALIDAS - revise cableado I2C (|a|=0.123 m/s2 |w|=4.50000 rad/s)"),
            now=0.1,
        ) == []

    def test_distinto_sensor_no_se_agrupa(self):
        c = EventCollapser(window_s=30.0)
        c.push(_event(), now=0.0)
        for i in range(1, 4):
            c.push(_event(ts_ms=1_000 + i), now=i * 0.1)
        out = c.push(_event(sensor="adxl345", ts_ms=2_000), now=1.0)
        assert len(out) == 2  # cierre de la ráfaga mpu6050 + aviso adxl345
        assert out[0][1] == " (×3 repeticiones)"
        assert out[1][0].sensor == "adxl345"
        assert out[1][1] == ""

    def test_sin_repeticiones_cambio_de_mensaje_no_genera_cierre(self):
        c = EventCollapser(window_s=30.0)
        c.push(_event(), now=0.0)
        out = c.push(_event(sensor="adxl345", ts_ms=2_000), now=0.1)
        assert len(out) == 1  # nada que cerrar: la ráfaga no acumuló conteo
        assert out[0][0].sensor == "adxl345"

    def test_evento_sin_code_usa_el_mensaje_como_clave(self):
        c = EventCollapser(window_s=30.0)
        c.push(_event(code=None), now=0.0)
        assert c.push(_event(code=None), now=0.1) == []
        out = c.push(_event(code=None, message="[OTRO] texto"), now=0.2)
        assert len(out) == 2

    def test_sin_repeticiones_no_hay_sufijo_nunca(self):
        c = EventCollapser(window_s=30.0)
        out = c.push(_event(), now=0.0)
        out += c.push(_event(code="I2C ERROR"), now=1.0)
        assert all(s == "" for _, s in out)

    def test_acumulacion_continua_emite_como_maximo_un_aviso_por_ventana(self):
        c = EventCollapser(window_s=30.0)
        published = c.push(_event(), now=0.0)
        # 1 hora de fallo continuo a 10 Hz: solo los agregados por ventana.
        # `now` en segundos enteros (float exacto): sin ruido de coma flotante
        # en el borde de la ventana.
        for sec in range(1, 3601):
            for _ in range(10):
                published += c.push(_event(ts_ms=sec), now=float(sec))
        # 1 inicial + 1 por cada ventana de 30 s en 1 h = 1 + 120.
        assert len(published) == 121
        assert all(s.endswith(" repeticiones)") for _, s in published[1:])


class TestNivelYCodeDelFirmware:
    """Las líneas del firmware deben parsear con el code/nivel esperados."""

    def test_muestras_invalidas_es_advertencia_con_code(self):
        parser = ProtocolParser()
        item = parser.parse(
            "[MPU6050] MUESTRAS INVALIDAS - revise cableado I2C "
            "(|a|=0.000 m/s2 |w|=0.00000 rad/s)"
        )
        assert isinstance(item, LogEvent)
        assert item.level == "warn"  # ámbar en la consola, no info
        assert item.code == "MUESTRAS INVALIDAS"
        assert item.sensor == "mpu6050"

    def test_i2c_error_sigue_siendo_error(self):
        parser = ProtocolParser()
        item = parser.parse("[MPU6050] I2C ERROR - sin respuesta en el bus")
        assert isinstance(item, LogEvent)
        assert item.level == "error"

    def test_ok_detectado_sigue_siendo_info(self):
        parser = ProtocolParser()
        item = parser.parse("[MPU6050] OK - Detectado en 0x68")
        assert isinstance(item, LogEvent)
        assert item.level == "info"

    def test_ds18b20_invalida_es_error(self):
        parser = ProtocolParser()
        item = parser.parse("[DS18B20] ERROR: lectura invalida o sensor desconectado")
        assert isinstance(item, LogEvent)
        assert item.level == "error"


class _FakeRepo:
    """Repositorio mínimo para probar el cableado de _handle_event."""

    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    def insert_event(self, event: LogEvent) -> None:
        self.events.append(event)

    def get_sensor_status_rows(self) -> dict:
        return {}


class TestCableadoEnIngest:
    def test_handle_event_guarda_y_publica_el_agregado(self):
        repo = _FakeRepo()
        published: list[dict] = []
        service = IngestService(repo, published.append, simulate=True)

        for i in range(10):
            service._handle_event(_event(ts_ms=1_000 + i))
        # Primera aparición publicada tal cual…
        assert len(repo.events) == 1
        assert repo.events[0].message.endswith("revise cableado I2C")

        # …mientras sigan dentro de la ventana se cuentan en silencio…
        service._handle_event(_event(ts_ms=2_000))
        assert len(repo.events) == 1  # aún dentro de la ventana de 30 s

        # …y al cerrarse la ventana sale UN agregado con las repeticiones
        # desde el primer aviso (12 ocurrencias en total: 1 + 11).
        service._events._last_pub -= 31.0  # fuerza el cierre de ventana
        service._handle_event(_event(ts_ms=3_000))
        assert len(repo.events) == 2
        assert repo.events[1].message.endswith("(×11 repeticiones)")
        assert published[-1]["line"] == repo.events[1].message
        assert published[-1]["type"] == "log"
        assert published[0]["line"].endswith("revise cableado I2C")


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
