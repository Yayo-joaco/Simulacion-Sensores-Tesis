"""Política de retención ejecutable del almacén de muestras.

Por qué existe: ``samples`` crece sin límite (~2,4 GB/día a 50 Hz x 2
sensores, ~780-870 GB/año; ver los cálculos en ``config.py`` y en la sección
de retención de ``ARQUITECTURA.md``). Este módulo implementa la política que
acota ese crecimiento y la ejecuta como tarea de fondo sin jamás tumbar la
ingesta (contrato §6).

Tres mecanismos, en este orden dentro de cada pasada:

1. **Agregación histórica** (``AGGREGATE_AFTER_DAYS``, 7 días): el detalle más
   viejo que el horizonte se reduce a ``samples_agg`` (promedio/min/max/n por
   bucket de ``AGGREGATE_BUCKET_MIN``) y se borra de ``samples``, en la misma
   transacción. Es lo que permite conservar tendencias de un año ocupando
   ~0,5 GB en lugar de ~780 GB.
2. **Retención por edad** (``RETENTION_DAYS``, 30 días): red de seguridad que
   garantiza que nada supere la edad máxima aunque la agregación haya fallado
   o esté mal configurada (p. ej. ``AGGREGATE_AFTER_DAYS > RETENTION_DAYS``).
   En régimen normal no borra nada, porque la agregación ya lo hizo.
3. **Techo de filas** (``RETENTION_MAX_ROWS``, 5 millones): expulsa las
   muestras más antiguas por encima del techo aunque no tengan la edad. Es el
   que manda a la tasa de diseño (5 M filas ≈ 13,9 h a 100 filas/s). Lo que
   expulsa se agrega ANTES de borrarse: el techo acota el disco, nunca
   destruye tendencias.

Regla de oro de la orquestación: **nada se elimina sin agregarse antes**. El
corte de agregación de cada pasada es el más agresivo de los tres límites
(horizonte de agregación, edad, frontera del techo), de modo que cualquier
fila que vaya a morir pasa primero por ``samples_agg``.

Vacuum periódico opcional (``RETENTION_VACUUM_INTERVAL_S``):

* ``PRAGMA incremental_vacuum`` devuelve páginas al SO solo si la base se creó
  con ``auto_vacuum=INCREMENTAL`` (lo hace ``db.init_schema`` en bases
  nuevas); con bases antiguas es un no-op y se avisa en el log.
* ``VACUUM`` completo es opt-in (``RETENTION_VACUUM_FULL``) porque bloquea
  todas las escrituras durante la copia y necesita hasta 2x el tamaño del
  archivo en disco libre.

Tolerancia a fallos: cada paso se protege con ``try/except`` y solo genera un
log; la tarea de fondo nunca termina por una excepción ni propagaría nada a la
ingesta. La política vive en memoria (sin tabla de control) a propósito: no
hay estado que corromper y reejecutar una pasada es inocuo (el upsert de
agregados fusiona estadísticas en lugar de duplicarlas).
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import config
from .db import Repository

log = logging.getLogger(__name__)

_MS_PER_DAY = 86_400_000


class RetentionPolicy:
    """Ejecuta las pasadas de retención sobre un :class:`~server.db.Repository`.

    Todos los parámetros son opcionales y por defecto salen de ``config``
    (que a su vez admite variables de entorno ``MONITOREO_*``); ``run.py``
    los sobrescribe con banderas de línea de comandos.
    """

    def __init__(
        self,
        repo: Repository,
        *,
        retention_days: int | None = None,
        max_rows: int | None = None,
        aggregate_after_days: int | None = None,
        bucket_min: int | None = None,
        interval_s: float | None = None,
        vacuum_interval_s: float | None = None,
        vacuum_full: bool | None = None,
    ) -> None:
        self.repo = repo
        self.retention_days = (
            config.RETENTION_DAYS if retention_days is None else int(retention_days)
        )
        self.max_rows = config.RETENTION_MAX_ROWS if max_rows is None else int(max_rows)
        self.aggregate_after_days = (
            config.AGGREGATE_AFTER_DAYS
            if aggregate_after_days is None
            else int(aggregate_after_days)
        )
        self.bucket_min = (
            config.AGGREGATE_BUCKET_MIN if bucket_min is None else int(bucket_min)
        )
        self.interval_s = (
            config.RETENTION_INTERVAL_S if interval_s is None else float(interval_s)
        )
        self.vacuum_interval_s = (
            config.RETENTION_VACUUM_INTERVAL_S
            if vacuum_interval_s is None
            else float(vacuum_interval_s)
        )
        self.vacuum_full = (
            config.RETENTION_VACUUM_FULL if vacuum_full is None else bool(vacuum_full)
        )
        if self.bucket_min <= 0:
            raise ValueError("bucket_min debe ser > 0")
        self.bucket_ms = self.bucket_min * 60_000

        # Estado observable desde GET /api/db/stats.
        self.last_summary: dict | None = None
        self.next_purge_ts_ms: int | None = None
        self._last_vacuum_ms: int = 0

    # ------------------------------------------------------------------
    # Pasada única
    # ------------------------------------------------------------------

    def run_once(self) -> dict:
        """Ejecuta una pasada completa. Nunca lanza excepciones.

        Devuelve un resumen con lo que hizo cada paso; si algo falló, el fallo
        queda registrado en ``errors`` y en el log, y el resto de los pasos se
        ejecuta igualmente. Un fallo de purga jamás debe tumbar la ingesta.
        """
        now_ms = int(time.time() * 1000)
        summary: dict = {
            "started_ts_ms": now_ms,
            "finished_ts_ms": None,
            "samples_before": None,
            "samples_after": None,
            "aggregated_rows": 0,
            "agg_rows_written": 0,
            "purged_by_age": 0,
            "purged_by_cap": 0,
            "vacuum": "skipped",
            "errors": [],
        }
        try:
            self._run_pass(summary)
        except Exception:  # pragma: no cover - red de seguridad final
            log.exception("Pasada de retención interrumpida; la ingesta continúa")
            summary["errors"].append("fallo inesperado")
        summary["finished_ts_ms"] = int(time.time() * 1000)
        self.last_summary = summary

        if summary["errors"]:
            log.warning("Retención con errores: %s", summary)
        else:
            log.info(
                "Retención: agregadas %d filas (%d en samples_agg), "
                "purga edad %d, techo %d, vacuum=%s, samples %s -> %s",
                summary["aggregated_rows"],
                summary["agg_rows_written"],
                summary["purged_by_age"],
                summary["purged_by_cap"],
                summary["vacuum"],
                summary["samples_before"],
                summary["samples_after"],
            )
        return summary

    def _run_pass(self, summary: dict) -> None:
        now_ms = int(time.time() * 1000)
        horizon_ms = now_ms - self.aggregate_after_days * _MS_PER_DAY
        age_ms = now_ms - self.retention_days * _MS_PER_DAY

        # -- 3. frontera del techo de filas (se calcula primero porque puede
        #        ser el corte más agresivo de todos) ------------------------
        cap_cutoff: int | None = None
        try:
            total = self.repo.count_samples()
            summary["samples_before"] = total
            if self.max_rows > 0 and total > self.max_rows:
                boundary = self.repo.oldest_ts_boundary(total - self.max_rows)
                if boundary is not None:
                    # +1: downsample_before usa corte exclusivo (ts < cutoff)
                    # y la frontera es el ts del último registro que sale.
                    cap_cutoff = boundary + 1
        except Exception:
            log.exception("No se pudo evaluar el techo de filas")
            summary["errors"].append("techo de filas")

        # -- 1. agregación: se agrega TODO lo que se va a borrar ----------
        # El corte es el más agresivo de los tres límites. Así, nada muere
        # sin pasar antes por samples_agg, sea por edad, por techo o por su
        # propia ventana de agregación.
        agg_cutoff = max(horizon_ms, age_ms)
        if cap_cutoff is not None:
            agg_cutoff = max(agg_cutoff, cap_cutoff)

        try:
            rows, agg_rows = self.repo.downsample_before(agg_cutoff, self.bucket_ms)
            summary["aggregated_rows"] += rows
            summary["agg_rows_written"] += agg_rows
            if cap_cutoff is not None and cap_cutoff > max(horizon_ms, age_ms):
                # Todo lo procesado en esta tanda salió por el techo.
                summary["purged_by_cap"] += rows
        except Exception:
            log.exception("Agregación fallida; se intenta la purga por edad igualmente")
            summary["errors"].append("agregación")

        # -- 2. retención por edad (red de seguridad) ---------------------
        try:
            summary["purged_by_age"] += self.repo.purge_older_than(age_ms)
        except Exception:
            log.exception("Purga por edad fallida")
            summary["errors"].append("purga por edad")

        # -- 4. vacuum opcional -------------------------------------------
        try:
            summary["vacuum"] = self._maybe_vacuum(now_ms)
        except Exception:
            log.exception("Vacuum fallido")
            summary["errors"].append("vacuum")

        try:
            summary["samples_after"] = self.repo.count_samples()
        except Exception:  # pragma: no cover - solo lectura de salud
            log.exception("No se pudo contar las muestras finales")

    # ------------------------------------------------------------------
    # Vacuum (devolver espacio al SO)
    # ------------------------------------------------------------------

    def _maybe_vacuum(self, now_ms: int) -> str:
        if self.vacuum_interval_s <= 0:
            return "disabled"
        if self._last_vacuum_ms and (now_ms - self._last_vacuum_ms) < (
            self.vacuum_interval_s * 1000
        ):
            return "skipped"
        self._last_vacuum_ms = now_ms

        if self.repo.auto_vacuum_mode() == 2:
            # Incremental: retira páginas libres del final del archivo en
            # tandas, reteniendo el cerrojo de escritura solo por tanda.
            self.repo.incremental_vacuum(256)
            log.info(
                "Vacuum incremental: %d páginas libres restantes",
                self.repo.freelist_pages(),
            )
            return "incremental"

        if self.vacuum_full:
            # Solo con bandera explícita: bloquea las escrituras durante toda
            # la copia de la base (ver docstring del módulo).
            log.warning(
                "VACUUM completo: las escrituras quedarán bloqueadas durante "
                "la copia de la base; no hacerlo con ingesta activa si el "
                "archivo es grande"
            )
            self.repo.full_vacuum()
            return "full"

        log.info(
            "Auto-vacuum no incremental en esta base (creada antes de la "
            "política): %d páginas libres se reutilizan pero no se devuelven "
            "al SO. Para comprimirla: --vacuum-full en ventana de "
            "mantenimiento, o recrear la base con auto_vacuum=INCREMENTAL",
            self.repo.freelist_pages(),
        )
        return "unavailable"

    # ------------------------------------------------------------------
    # Tarea de fondo
    # ------------------------------------------------------------------

    async def run_loop(self) -> None:
        """Tarea asyncio: pasada al arrancar y después cada ``interval_s``.

        El trabajo de SQLite es bloqueante y pesado, así que se ejecuta con
        ``asyncio.to_thread``: el event loop sigue sirviendo WebSocket y HTTP
        mientras se purga (requisito del contrato §6). Cualquier excepción se
        registra y el bucle continúa con la siguiente pasada.
        """
        log.info(
            "Retención activada: detalle %d días / techo %d filas / "
            "agregado a partir de %d días en buckets de %d min / cada %s s",
            self.retention_days,
            self.max_rows,
            self.aggregate_after_days,
            self.bucket_min,
            self.interval_s,
        )
        while True:
            self.next_purge_ts_ms = int(time.time() * 1000) + int(self.interval_s * 1000)
            try:
                await asyncio.to_thread(self.run_once)
            except asyncio.CancelledError:
                # Apagado del servidor: la pasada en curso se abandona con el
                # hilo; run_once protege cada paso y no dejará nada a medias
                # peor que una ventana parcialmente agregada (reinoculable).
                raise
            except Exception:  # pragma: no cover - run_once ya no lanza
                log.exception("Error inesperado en la tarea de retención")
            await asyncio.sleep(self.interval_s)
