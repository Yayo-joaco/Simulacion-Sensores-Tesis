/*
 * charts.js — Renderizador de series temporales sobre <canvas>, sin librerías.
 *
 * ¿Por qué canvas y no SVG? El requisito del contrato (sección 6) es mantener
 * 60 fps con 2 sensores a 50 Hz: son ~300 puntos nuevos por segundo y varias
 * series dibujadas a la vez. En SVG cada punto sería un nodo del DOM y el
 * coste de actualización crecería con el tiempo; en canvas se repinta una sola
 * superficie y el coste por frame es constante.
 *
 * ¿Por qué un búfer circular por serie? Un array que crece sin límite termina
 * por agotar la memoria durante una sesión larga de telemetría. Aquí cada serie
 * reserva ~3000 puntos (60 s a 50 Hz) y reutiliza la memoria al sobrescribir
 * los valores más antiguos.
 *
 * Decisiones visuales (metodología de visualización aplicada):
 *  - Líneas de 2 px con extremos redondeados: la señal manda, el resto de la
 *    gráfica (rejilla, ejes) es deliberadamente apagado.
 *  - Rejilla de trazo SOLIDO y fino de un gris a un paso del fondo: una rejilla
 *    discontinua se confunde con "proyección" o con la línea de umbral.
 *  - La línea de umbral SÍ va discontinua: su significado literal es
 *    "referencia / límite", que es justo para lo que se reservó el trazo
 *    discontinuo.
 *  - El texto nunca lleva el color de la serie (las series se identifican por
 *    un trazo de color junto al texto, no coloreando el texto).
 *  - Toda gráfica con ≥2 series lleva leyenda con casillas de verificación
 *    para mostrar/ocultar; el color sigue a la entidad (X siempre el mismo
 *    color aunque se oculten otras series).
 *  - Capa de interacción por defecto: crosshair vertical que se ajusta al
 *    punto más cercano + tooltip único con todas las series en ese instante.
 *    El teclado (flechas) mueve el mismo crosshair.
 *  - Vista de tabla accesible por gráfica: el tooltip nunca es la única vía
 *    para leer un valor.
 *
 * El color NO se define aquí: cada gráfica recibe los colores de sus series
 * por parámetro (paleta validada de la aplicación).
 */

(function (global) {
  "use strict";

  // ───────────────────────────── utilidades ─────────────────────────────────

  // Fuente del sistema: sin descargas, sin CDN (el sistema debe funcionar sin
  // Internet en campo). tabular-nums solo para cifras alineadas (ticks/tablas).
  const FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif';

  const chrome = {
    surface: "#121b2e",      // superficie de la tarjeta / fondo del canvas
    grid: "#223150",         // rejilla: un paso por encima del fondo, hairline
    axis: "#2e4066",         // eje base: ligeramente más visible que la rejilla
    textPrimary: "#e8eefc",  // tinta principal
    textSecondary: "#a7b4cc", // tinta secundaria
    textMuted: "#7387a8",    // ejes y etiquetas: apagados a propósito
  };

  /** Formatea un instante epoch-ms en HH:MM:SS.mmm (UTC, coherente con el
   *  ts_ms del contrato, que es epoch UTC en milisegundos). */
  function formatTimeUTC(ms, withMillis) {
    const d = new Date(ms);
    const p = (n, w) => String(n).padStart(w || 2, "0");
    const base = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
    return withMillis ? `${base}.${p(d.getUTCMilliseconds(), 3)}` : base;
  }

  /** Número "bonito" para los ticks del eje: 1/2/5 × 10ⁿ. */
  function niceStep(raw) {
    if (!(raw > 0)) return 1;
    const exp = Math.floor(Math.log10(raw));
    const frac = raw / Math.pow(10, exp);
    const nice = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10;
    return nice * Math.pow(10, exp);
  }

  /** Ticks del eje Y dentro de [min, max] redondeados a valores legibles. */
  function niceTicks(min, max, target) {
    const step = niceStep((max - min) / Math.max(1, target));
    const ticks = [];
    for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-6; v += step) {
      ticks.push(Math.abs(v) < step * 1e-9 ? 0 : v); // evita -0
    }
    return { ticks, step };
  }

  /** Ticks del eje X (tiempo) en segundos "redondos". */
  function timeTicks(minMs, maxMs) {
    const spanS = (maxMs - minMs) / 1000;
    const stepS = niceStep(spanS / 5);
    const ticks = [];
    const start = Math.ceil(minMs / 1000 / stepS) * stepS * 1000;
    for (let t = start; t <= maxMs; t += stepS * 1000) ticks.push(t);
    return ticks;
  }

  /** Interpolación exponencial: suaviza el desplazamiento del eje temporal y
   *  los saltos de la escala Y para que la gráfica no "parpadee" a 50 Hz. */
  function lerp(cur, target, rate) {
    return cur + (target - cur) * rate;
  }

  // ─────────────────────────── búfer circular ───────────────────────────────

  /**
   * RingSeries — puntos (t, v) de una sola serie en arrays tipados.
   * Capacidad fija: al llenarse, el punto más antiguo se sobrescribe.
   */
  class RingSeries {
    constructor(capacity) {
      this.capacity = capacity;
      this.t = new Float64Array(capacity); // epoch ms
      this.v = new Float64Array(capacity);
      this.head = 0;   // próxima posición a escribir
      this.count = 0;  // puntos válidos
    }

    push(t, v) {
      this.t[this.head] = t;
      this.v[this.head] = v;
      this.head = (this.head + 1) % this.capacity;
      if (this.count < this.capacity) this.count++;
    }

    /** Carga inicial (histórico): misma lógica, pero en lote. */
    load(points) {
      for (const p of points) this.push(p[0], p[1]);
    }

    clear() {
      this.head = 0;
      this.count = 0;
    }

    /** Índice del primer punto (el más antiguo). */
    firstIndex() {
      return this.count < this.capacity ? 0 : this.head;
    }

    /** Acceso aleatorio al i-ésimo punto en orden cronológico. */
    at(i) {
      return {
        t: this.t[(this.firstIndex() + i) % this.capacity],
        v: this.v[(this.firstIndex() + i) % this.capacity],
      };
    }

    get last() {
      return this.count ? this.at(this.count - 1) : null;
    }

    /** Búsqueda binaria del índice cuyo t es el más cercano a timeMs. */
    nearestIndex(timeMs) {
      if (!this.count) return -1;
      let lo = 0, hi = this.count - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (this.at(mid).t < timeMs) lo = mid + 1; else hi = mid;
      }
      const cand = this.at(lo);
      const prev = lo > 0 ? this.at(lo - 1) : cand;
      // Ojo con lo === 0: cand y prev serían el mismo punto y la comparación
      // estricta devolvería -1. Se elige siempre el candidato en ese caso.
      return (lo > 0 && Math.abs(prev.t - timeMs) < Math.abs(cand.t - timeMs)) ? lo - 1 : lo;
    }
  }

  // ───────────────────────────── gráfica base ───────────────────────────────

  let chartSeq = 0;

  class TimeSeriesChart {
    /**
     * @param {object} opts
     *  canvas        : <canvas> donde pintar
     *  legendEl      : contenedor de la leyenda (se genera con checkboxes)
     *  tooltipEl     : contenedor del tooltip (si falta, se crea uno)
     *  title         : nombre accesible de la gráfica
     *  yUnit         : unidad del eje Y ("m/s²", "°/s", ...)
     *  windowMs      : número o función -> anchura de la ventana temporal
     *  series        : [{ key, label, color }]
     *  thresholds    : [{ key, label, color }] (valor con setThreshold)
     *  decimals      : decimales al formatear valores
     *  capacity      : puntos por serie (defecto 3000)
     */
    constructor(opts) {
      this.id = "chart-" + (++chartSeq);
      this.canvas = opts.canvas;
      this.ctx = this.canvas.getContext("2d");
      this.legendEl = opts.legendEl || null;
      this.title = opts.title || "Serie temporal";
      this.yUnit = opts.yUnit || "";
      this.windowMsFn = typeof opts.windowMs === "function" ? opts.windowMs : () => opts.windowMs || 60000;
      this.decimals = opts.decimals ?? 2;
      this.capacity = opts.capacity || 3000;

      this.series = opts.series.map((s) => ({
        key: s.key,
        label: s.label,
        color: s.color,
        visible: true,
        data: new RingSeries(this.capacity),
      }));

      this.thresholds = (opts.thresholds || []).map((th) => ({
        key: th.key,
        label: th.label,
        color: th.color,
        value: null,
      }));

      // Estado de la vista: se animan hacia su objetivo para que el scroll
      // temporal sea continuo en vez de un salto por cada muestra.
      this.viewEnd = null;      // borde derecho de la ventana (epoch ms)
      this.yMin = null; this.yMax = null;
      this.hoverIndex = -1;     // índice del crosshair sobre la serie primaria
      this.hoverTimeMs = null;
      this._dirty = true;
      this._lastRender = 0;

      this._initTooltip(opts.tooltipEl);
      this._initLegend();
      this._initInteraction();
      installRenderLoop(this);
    }

    // ─────────────────────────── datos ─────────────────────────────────────

    /** Inserta un valor de una serie (ya en unidades de presentación). */
    push(key, t, v) {
      const s = this.series.find((x) => x.key === key);
      if (!s || !Number.isFinite(t) || !Number.isFinite(v)) return;
      s.data.push(t, v);
      this._dirty = true;
    }

    /** Carga masiva (histórico REST): points = [[ts_ms, v], ...] */
    loadHistory(key, points) {
      const s = this.series.find((x) => x.key === key);
      if (!s || !Array.isArray(points)) return;
      s.data.load(points.filter((p) => Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1])));
      this._dirty = true;
    }

    setThreshold(key, value) {
      const th = this.thresholds.find((x) => x.key === key);
      if (!th) return;
      th.value = Number.isFinite(value) ? value : null;
      this._dirty = true;
    }

    clear() {
      for (const s of this.series) s.data.clear();
      this._dirty = true;
    }

    // ─────────────────────────── leyenda ───────────────────────────────────

    _initLegend() {
      if (!this.legendEl) return;
      this.legendEl.classList.add("chart-legend");
      for (const s of this.series) {
        const label = document.createElement("label");
        label.className = "legend-item";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = true;
        cb.setAttribute("aria-label", `Mostrar serie ${s.label}`);
        cb.addEventListener("change", () => {
          s.visible = cb.checked;
          this._dirty = true;
        });
        const key = document.createElement("span");
        key.className = "legend-key";
        key.style.borderTopColor = s.color; // la identidad va en el trazo, no en el texto
        const txt = document.createElement("span");
        txt.className = "legend-label";
        txt.textContent = s.label;
        label.append(cb, key, txt);
        this.legendEl.appendChild(label);
      }
    }

    // ─────────────────────────── tooltip ───────────────────────────────────

    _initTooltip(existing) {
      const wrap = this.canvas.parentElement;
      if (wrap) wrap.classList.add("chart-wrap");
      this.tooltipEl = existing || document.createElement("div");
      this.tooltipEl.className = "chart-tooltip";
      this.tooltipEl.hidden = true;
      this.tooltipEl.setAttribute("role", "status");
      if (this.tooltipEl.parentElement !== wrap && wrap) wrap.appendChild(this.tooltipEl);
    }

    _updateTooltip() {
      const tip = this.tooltipEl;
      if (this.hoverIndex < 0 || !this.hoverTimeMs) {
        tip.hidden = true;
        return;
      }
      // Un solo tooltip con TODAS las series visibles en ese instante: el
      // lector apunta al tiempo, nunca a una línea de 2 px.
      tip.textContent = "";
      const head = document.createElement("div");
      head.className = "tooltip-time";
      head.textContent = `${formatTimeUTC(this.hoverTimeMs, true)} UTC`;
      tip.appendChild(head);

      const primary = this.series.find((s) => s.visible && s.data.count) || this.series[0];
      for (const s of this.series) {
        if (!s.visible || !s.data.count) continue;
        const idx = s === primary ? this.hoverIndex : s.data.nearestIndex(this.hoverTimeMs);
        if (idx < 0) continue;
        const pt = s.data.at(idx);
        const row = document.createElement("div");
        row.className = "tooltip-row";
        const key = document.createElement("span");
        key.className = "legend-key";
        key.style.borderTopColor = s.color;
        // El valor manda (fuerte), la etiqueta acompaña (secundaria).
        const val = document.createElement("span");
        val.className = "tooltip-value";
        val.textContent = pt.v.toFixed(this.decimals);
        const lbl = document.createElement("span");
        lbl.className = "tooltip-label";
        lbl.textContent = s.label;
        row.append(key, val, lbl);
        tip.appendChild(row);
      }
      if (this.yUnit) {
        const foot = document.createElement("div");
        foot.className = "tooltip-unit";
        foot.textContent = `unidad: ${this.yUnit}`;
        tip.appendChild(foot);
      }
      tip.hidden = false;

      // Posición: junto al crosshair, sin salirse del marco.
      const rect = this.canvas.getBoundingClientRect();
      const x = this.xForTime(this.hoverTimeMs);
      const flip = x > rect.width * 0.62;
      tip.style.left = flip ? "" : `${Math.min(x + 14, rect.width - 180)}px`;
      tip.style.right = flip ? `${Math.max(rect.width - x + 14, 8)}px` : "";
      tip.style.top = "10px";
    }

    // ────────────────────────── interacción ────────────────────────────────

    _initInteraction() {
      const c = this.canvas;
      c.tabIndex = 0; // el mismo crosshair es operable con teclado
      c.setAttribute("role", "img");
      c.setAttribute("aria-label", `${this.title}. Eje vertical en ${this.yUnit || "unidades"}. Use las flechas para recorrer los valores; hay una vista de tabla disponible.`);

      const pick = (ev) => {
        const rect = c.getBoundingClientRect();
        const x = ev.clientX - rect.left;
        this._hoverToTime(this.timeForX(x));
      };
      c.addEventListener("pointermove", pick);
      c.addEventListener("pointerleave", () => {
        this.hoverIndex = -1;
        this.hoverTimeMs = null;
        this._dirty = true;
      });
      c.addEventListener("keydown", (ev) => {
        const primary = this.series.find((s) => s.visible && s.data.count);
        if (!primary) return;
        if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
          ev.preventDefault();
          const delta = ev.key === "ArrowRight" ? 1 : -1;
          let idx = this.hoverIndex >= 0 ? this.hoverIndex + delta : primary.data.count - 1;
          idx = Math.max(0, Math.min(primary.data.count - 1, idx));
          this.hoverIndex = idx;
          this.hoverTimeMs = primary.data.at(idx).t;
          this._dirty = true;
        } else if (ev.key === "Escape") {
          this.hoverIndex = -1;
          this.hoverTimeMs = null;
          this._dirty = true;
        }
      });
    }

    _hoverToTime(t) {
      const primary = this.series.find((s) => s.visible && s.data.count);
      if (!primary) return;
      const idx = primary.data.nearestIndex(t);
      if (idx < 0) return;
      this.hoverIndex = idx;
      this.hoverTimeMs = primary.data.at(idx).t;
      this._dirty = true;
    }

    // ───────────────────────── coordenadas ────────────────────────────────

    _plotRect() {
      // Margen izquierdo para los ticks Y, inferior para la banda de tiempo:
      // el contenedor incluye la banda para que nada se recorte.
      const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
      const left = 52, right = 14, top = 22, bottom = 26;
      return { x: left, y: top, w: Math.max(10, w - left - right), h: Math.max(10, h - top - bottom) };
    }

    xForTime(t) {
      const r = this._plotRect();
      const span = this.windowMsFn() || 60000;
      return r.x + ((t - (this.viewEnd - span)) / span) * r.w;
    }

    timeForX(x) {
      const r = this._plotRect();
      const span = this.windowMsFn() || 60000;
      return (this.viewEnd - span) + ((x - r.x) / r.w) * span;
    }

    // ──────────────────────────── pintado ─────────────────────────────────

    needsRender() {
      // Sigue redibujando mientras la vista esté animándose; si no, solo
      // cuando hay datos nuevos o interacción. Ahorra frames en reposo.
      const animating = this.viewEnd !== null && this._targetEnd !== this.viewEnd;
      return this._dirty || animating;
    }

    render(now) {
      this._dirty = false;
      const canvas = this.canvas, ctx = this.ctx;
      const dpr = global.devicePixelRatio || 1;
      const w = canvas.clientWidth, h = canvas.clientHeight;
      if (w === 0 || h === 0) return;
      if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = chrome.surface;
      ctx.fillRect(0, 0, w, h);

      // Última marca de tiempo entre todas las series visibles.
      let latest = -Infinity;
      for (const s of this.series) {
        if (!s.visible || !s.data.count) continue;
        const t = s.data.last.t;
        if (t > latest) latest = t;
      }
      if (!Number.isFinite(latest)) {
        this._drawEmpty("Esperando datos…");
        this._updateTooltip();
        return;
      }

      const span = this.windowMsFn() || 60000;
      this._targetEnd = latest;
      if (this.viewEnd === null) this.viewEnd = latest;
      // Scroll temporal suave: el borde derecho persigue la última muestra.
      this.viewEnd = lerp(this.viewEnd, latest, 0.18);
      if (Math.abs(this.viewEnd - latest) < 1) this.viewEnd = latest;

      const r = this._plotRect();
      const t0 = this.viewEnd - span, t1 = this.viewEnd;

      // Escala Y: rango de los datos visibles + umbrales, con un margen y
      // suavizado para que los saltos de escala no desorienten.
      let lo = Infinity, hi = -Infinity;
      for (const s of this.series) {
        if (!s.visible || !s.data.count) continue;
        const idx0 = Math.max(0, s.data.nearestIndex(t0) - 1);
        for (let i = idx0; i < s.data.count; i++) {
          const p = s.data.at(i);
          if (p.t < t0) continue;
          if (p.t > t1) break;
          if (p.v < lo) lo = p.v;
          if (p.v > hi) hi = p.v;
        }
      }
      for (const th of this.thresholds) {
        if (th.value === null) continue;
        // El umbral forma parte del rango a propósito: la línea debe verse
        // siempre que su valor sea finito, incluso si aleja la escala.
        if (th.value < lo) lo = th.value;
        if (th.value > hi) hi = th.value;
      }
      if (!Number.isFinite(lo) || !Number.isFinite(hi)) {
        this._drawEmpty("Esperando datos…");
        this._updateTooltip();
        return;
      }
      if (hi - lo < 1e-9) { hi += 0.5; lo -= 0.5; }
      const pad = (hi - lo) * 0.10;
      const targetLo = lo - pad, targetHi = hi + pad;
      if (this.yMin === null) { this.yMin = targetLo; this.yMax = targetHi; }
      this.yMin = lerp(this.yMin, targetLo, 0.15);
      this.yMax = lerp(this.yMax, targetHi, 0.15);

      const yFor = (v) => r.y + r.h - ((v - this.yMin) / (this.yMax - this.yMin)) * r.h;

      // ── rejilla + ejes (hairline sólido, apagado) ──
      const yt = niceTicks(this.yMin, this.yMax, 5);
      ctx.lineWidth = 1;
      ctx.font = `11px ${FONT}`;
      ctx.textBaseline = "middle";
      for (const v of yt.ticks) {
        const y = Math.round(yFor(v)) + 0.5;
        if (y < r.y - 1 || y > r.y + r.h + 1) continue;
        ctx.strokeStyle = chrome.grid;
        ctx.beginPath();
        ctx.moveTo(r.x, y);
        ctx.lineTo(r.x + r.w, y);
        ctx.stroke();
        ctx.fillStyle = chrome.textMuted;
        ctx.textAlign = "right";
        // tabular-nums: los ticks son cifras que deben alinearse en columna
        ctx.font = `11px ${FONT}`;
        ctx.fillText(v.toFixed(yt.step < 0.1 ? 2 : yt.step < 1 ? 1 : 0), r.x - 8, y);
      }
      // eje X
      const xt = timeTicks(t0, t1);
      ctx.textBaseline = "top";
      for (const t of xt) {
        const x = Math.round(this.xForTime(t)) + 0.5;
        if (x < r.x - 1 || x > r.x + r.w + 1) continue;
        ctx.strokeStyle = chrome.grid;
        ctx.beginPath();
        ctx.moveTo(x, r.y);
        ctx.lineTo(x, r.y + r.h);
        ctx.stroke();
        ctx.fillStyle = chrome.textMuted;
        ctx.textAlign = "center";
        ctx.fillText(formatTimeUTC(t), x, r.y + r.h + 6);
      }
      // marco del área de trazado
      ctx.strokeStyle = chrome.axis;
      ctx.beginPath();
      ctx.moveTo(r.x + 0.5, r.y);
      ctx.lineTo(r.x + 0.5, r.y + r.h + 0.5);
      ctx.lineTo(r.x + r.w, r.y + r.h + 0.5);
      ctx.stroke();

      // unidad del eje Y (arriba a la izquierda; el título ya está en la tarjeta)
      ctx.fillStyle = chrome.textMuted;
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText(this.yUnit, r.x, 4);

      // ── umbrales (referencia discontinua + etiqueta con chip) ──
      for (const th of this.thresholds) {
        if (th.value === null) continue;
        const y = yFor(th.value);
        if (y < r.y || y > r.y + r.h) continue;
        ctx.save();
        ctx.setLineDash([6, 4]);
        ctx.strokeStyle = th.color;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(r.x, y);
        ctx.lineTo(r.x + r.w, y);
        ctx.stroke();
        ctx.restore();
        const label = `${th.label} ${th.value.toFixed(this.decimals)}`;
        ctx.font = `10px ${FONT}`;
        const tw = ctx.measureText(label).width + 10;
        ctx.fillStyle = chrome.surface;
        ctx.fillRect(r.x + r.w - tw, y - 15, tw, 13);
        ctx.fillStyle = chrome.textSecondary;
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";
        ctx.fillText(label, r.x + r.w - 5, y - 8);
      }

      // ── series ──
      ctx.save();
      ctx.beginPath();
      ctx.rect(r.x, r.y, r.w, r.h);
      ctx.clip();
      for (const s of this.series) {
        if (!s.visible || s.data.count < 1) continue;
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 2;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.beginPath();
        let penDown = false;
        let prevT = null;
        const startIdx = Math.max(0, s.data.nearestIndex(t0) - 1);
        for (let i = startIdx; i < s.data.count; i++) {
          const p = s.data.at(i);
          if (p.t > t1 + span * 0.02) break;
          const x = this.xForTime(p.t), y = yFor(p.v);
          // Una interrupción de datos (reinicio del ESP32, caída del puerto)
          // se dibuja como hueco, no como una diagonal falsa entre tramos.
          const gap = prevT !== null && p.t - prevT > 1000;
          if (!penDown || gap) { ctx.moveTo(x, y); penDown = true; }
          else ctx.lineTo(x, y);
          prevT = p.t;
        }
        ctx.stroke();
      }
      ctx.restore();

      // ── crosshair + puntos con anillo de superficie ──
      if (this.hoverIndex >= 0 && this.hoverTimeMs !== null) {
        const x = Math.round(this.xForTime(this.hoverTimeMs)) + 0.5;
        if (x >= r.x && x <= r.x + r.w) {
          ctx.strokeStyle = chrome.textMuted;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(x, r.y);
          ctx.lineTo(x, r.y + r.h);
          ctx.stroke();
          for (const s of this.series) {
            if (!s.visible || !s.data.count) continue;
            const idx = s.data.nearestIndex(this.hoverTimeMs);
            if (idx < 0) continue;
            const p = s.data.at(idx);
            // El punto se dibuja en la X real de su muestra: las series pueden
            // no compartir instante exacto (p.ej. tras cargar el histórico).
            const px = Math.round(this.xForTime(p.t)) + 0.5;
            const y = yFor(p.v);
            // anillo del color de la superficie: el punto no se pierde donde
            // las líneas se cruzan
            ctx.beginPath();
            ctx.arc(px, y, 5, 0, Math.PI * 2);
            ctx.fillStyle = s.color;
            ctx.fill();
            ctx.lineWidth = 2;
            ctx.strokeStyle = chrome.surface;
            ctx.stroke();
          }
        }
      }

      this._updateTooltip();
    }

    _drawEmpty(msg) {
      const ctx = this.ctx;
      const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
      ctx.fillStyle = chrome.textMuted;
      ctx.font = `13px ${FONT}`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(msg, w / 2, h / 2);
    }

    // ──────────────────── vista de tabla (accesible) ───────────────────────

    /**
     * Rellena un <tbody> con hasta maxRows filas (t, valores). Es el gemelo
     * accesible de la gráfica: ningún valor depende del tooltip.
     */
    buildTable(headEl, bodyEl, maxRows) {
      const primary = this.series.find((s) => s.visible && s.data.count);
      headEl.textContent = "";
      bodyEl.textContent = "";
      const hr = document.createElement("tr");
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = "Hora (UTC)";
      hr.appendChild(th);
      for (const s of this.series) {
        if (!s.visible) continue;
        const c = document.createElement("th");
        c.scope = "col";
        c.textContent = `${s.label}${this.yUnit ? ` [${this.yUnit}]` : ""}`;
        hr.appendChild(c);
      }
      headEl.appendChild(hr);
      if (!primary) {
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.colSpan = 1 + this.series.length;
        td.textContent = "Sin datos.";
        tr.appendChild(td);
        bodyEl.appendChild(tr);
        return;
      }
      const n = primary.data.count;
      const rows = Math.min(maxRows || 30, n);
      const step = Math.max(1, Math.floor(n / rows));
      for (let i = n - rows * step; i < n; i += step) {
        const j = Math.max(0, i);
        const p = primary.data.at(j);
        const tr = document.createElement("tr");
        const tdT = document.createElement("td");
        tdT.textContent = formatTimeUTC(p.t, true);
        tr.appendChild(tdT);
        for (const s of this.series) {
          if (!s.visible) continue;
          const td = document.createElement("td");
          const idx = s.data.nearestIndex(p.t);
          td.textContent = idx >= 0 ? s.data.at(idx).v.toFixed(this.decimals) : "—";
          tr.appendChild(td);
        }
        bodyEl.appendChild(tr);
      }
    }
  }

  // ───────────────────────────── sparkline ─────────────────────────────────

  /**
   * Sparkline — tendencia de 12 puntos para las tarjetas KPI.
   * Sin ejes ni rejilla: es un acento de la tarjeta, no una gráfica completa.
   */
  class Sparkline {
    constructor(canvas, color, capacity) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.color = color;
      this.data = new RingSeries(capacity || 120);
      this._dirty = true;
      installRenderLoop(this);
    }

    push(t, v) {
      this.data.push(t, v);
      this._dirty = true;
    }

    needsRender() { return this._dirty; }

    render() {
      this._dirty = false;
      const canvas = this.canvas, ctx = this.ctx;
      const dpr = global.devicePixelRatio || 1;
      const w = canvas.clientWidth, h = canvas.clientHeight;
      if (w === 0 || h === 0) return;
      if (canvas.width !== Math.round(w * dpr)) canvas.width = Math.round(w * dpr);
      if (canvas.height !== Math.round(h * dpr)) canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      if (this.data.count < 2) return;
      let lo = Infinity, hi = -Infinity;
      for (let i = 0; i < this.data.count; i++) {
        const v = this.data.at(i).v;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
      if (hi - lo < 1e-9) { hi += 0.5; lo -= 0.5; }
      ctx.strokeStyle = this.color;
      ctx.lineWidth = 1.5;
      ctx.lineJoin = "round";
      ctx.beginPath();
      for (let i = 0; i < this.data.count; i++) {
        const p = this.data.at(i);
        const x = (i / (this.data.count - 1)) * (w - 2) + 1;
        const y = h - 2 - ((p.v - lo) / (hi - lo)) * (h - 4);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
  }

  // ──────────────────── visualización de inclinación ───────────────────────

  /**
   * InclinationWidget — horizonte artificial en miniatura.
   * Comunica roll/pitch de un vistazo sin competir con las gráficas de señal:
   * por eso usa rellenos planos y simbología hairline, sin degradados.
   */
  class InclinationWidget {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.roll = 0;  // grados (contrato: derived.roll en grados)
      this.pitch = 0; // grados
      this._dirty = true;
      installRenderLoop(this);
    }

    setOrientation(rollDeg, pitchDeg) {
      if (Number.isFinite(rollDeg)) this.roll = rollDeg;
      if (Number.isFinite(pitchDeg)) this.pitch = pitchDeg;
      this._dirty = true;
    }

    needsRender() { return this._dirty; }

    render() {
      this._dirty = false;
      const canvas = this.canvas, ctx = this.ctx;
      const dpr = global.devicePixelRatio || 1;
      const w = canvas.clientWidth, h = canvas.clientHeight;
      if (w === 0 || h === 0) return;
      if (canvas.width !== Math.round(w * dpr)) canvas.width = Math.round(w * dpr);
      if (canvas.height !== Math.round(h * dpr)) canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const cx = w / 2, cy = h / 2;
      const R = Math.min(w, h) / 2 - 4;
      const pitchPx = (this.pitch / 90) * R; // 90° ≈ radio completo

      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.clip();

      // Horizonte: rotado por roll, desplazado por pitch.
      ctx.translate(cx, cy);
      ctx.rotate((this.roll * Math.PI) / 180);
      ctx.fillStyle = "#1b2b47"; // "cielo": paso azulado apagado
      ctx.fillRect(-R * 2, -R * 2 - pitchPx, R * 4, R * 2 + pitchPx);
      ctx.fillStyle = "#26354f"; // "tierra"
      ctx.fillRect(-R * 2, -pitchPx, R * 4, R * 2 + pitchPx);
      // Línea de horizonte y marcas de pitch (hairline, sin ruido).
      ctx.strokeStyle = chrome.textMuted;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(-R, -pitchPx);
      ctx.lineTo(R, -pitchPx);
      ctx.stroke();
      for (const deg of [-20, -10, 10, 20]) {
        const y = -pitchPx - (deg / 90) * R;
        const half = deg % 20 === 0 ? R * 0.28 : R * 0.16;
        ctx.beginPath();
        ctx.moveTo(-half, y);
        ctx.lineTo(half, y);
        ctx.stroke();
      }
      ctx.restore();

      // Símbolo fijo de la aeronave/torre: referencia central.
      ctx.strokeStyle = "#22d3ee"; // acento de la interfaz (cian del brief)
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(cx - R * 0.45, cy);
      ctx.lineTo(cx - R * 0.12, cy);
      ctx.moveTo(cx + R * 0.12, cy);
      ctx.lineTo(cx + R * 0.45, cy);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(cx, cy, 2.5, 0, Math.PI * 2);
      ctx.fillStyle = "#22d3ee";
      ctx.fill();

      // Aro de recorte.
      ctx.strokeStyle = chrome.axis;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  // ─────────────────────── bucle de render compartido ──────────────────────

  const renderables = [];
  let loopStarted = false;

  function installRenderLoop(item) {
    renderables.push(item);
    if (loopStarted) return;
    loopStarted = true;
    // Un único requestAnimationFrame para todas las gráficas: se evita el
    // solapamiento de bucles y se mantiene el presupuesto de 16 ms por frame.
    const tick = () => {
      const now = performance.now();
      for (const r of renderables) {
        try {
          if (r.needsRender()) r.render(now);
        } catch (err) {
          // Una gráfica con datos corruptos no debe tumbar el dashboard entero.
          console.error("render:", err);
        }
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  global.TesisCharts = {
    RingSeries,
    TimeSeriesChart,
    Sparkline,
    InclinationWidget,
    formatTimeUTC,
    chrome,
  };
})(window);
