/*
 * app.js — Cliente WebSocket, gestión de estado y actualización de la UI.
 *
 * Origen de verdad del protocolo: CONTRATO_DE_DATOS.md (sección 4.2).
 * Los nombres de campos JSON se usan literalmente como los define el contrato
 * (raw.accel_*, raw.gyro_*, raw.temp_c, derived.roll/pitch/yaw,
 * derived.vib_rms/vib_peak, status.sensors[*].n_samples, log.line, ...).
 * Cualquier divergencia rompería la coincidencia con firmware y servidor.
 *
 * Reglas de datos que respeta este archivo:
 *  - raw.gyro_* llega en rad/s (así lo guarda la BD). La conversión a °/s se
 *    hace AQUÍ, al presentar, nunca se reescribe el valor del contrato.
 *  - derived.roll/pitch/yaw llegan en grados; se muestran tal cual.
 *  - ts_ms es epoch UTC en milisegundos: la latencia de ingesta se mide como
 *    Date.now() - ts_ms (reloj del navegador frente al del servidor).
 *
 * Robustez: si el WebSocket cae, se reconecta con retroceso exponencial y se
 * rellena el histórico vía GET /api/history para que ninguna gráfica arranque
 * vacía. Si no llegan datos, la UI queda en "esperando datos" sin romperse.
 */

(function () {
  "use strict";

  const { TimeSeriesChart, Sparkline, InclinationWidget, formatTimeUTC } = window.TesisCharts;

  // ─────────────────────────── paleta de series ────────────────────────────
  // Paleta categórica validada contra la superficie #121b2e (ver informe):
  // pasan banda de luminosidad, piso de croma, separación CVD (protan/deutan),
  // piso de visión normal y contraste ≥ 3:1. El orden es fijo y el color sigue
  // a la entidad: X siempre el slot 1, Y el slot 2, Z el slot 3.
  const SERIES = {
    x: "#0aa2c9", // slot 1 · familia cian
    y: "#7c3aed", // slot 2 · familia violeta
    z: "#047857", // slot 3 · familia verde
    rms: "#0aa2c9",   // vib_rms: slot 1 (primera serie del gráfico de vibración)
    peak: "#7c3aed",  // vib_peak: slot 2
  };
  // Colores de estado (reservados: nunca son identidad de serie).
  const STATUS = { warn: "#fbbf24", critical: "#f87171", good: "#34d399" };

  const RAD_TO_DEG = 180 / Math.PI; // contrato: BD en rad/s, presentación en °/s
  const VIB_THRESHOLD_KEY = "tesis.vib_threshold_mps2"; // localStorage
  const INCL_THRESHOLD_KEY = "tesis.incl_threshold_deg"; // localStorage (semáforo 3D)
  const MODEL3D_CFG_KEY = "tesis.model3d.geom"; // localStorage (geometría torre/antena)
  const MODEL3D_STALE_MS = 3000; // sin muestras en 3 s: modelo quieto + aviso
  const EVENTS_MAX = 300; // límite de líneas en memoria de la consola

  // ─────────────────────────────── estado ──────────────────────────────────

  const state = {
    ws: null,
    wsOpen: false,
    reconnectAttempts: 0,
    lastSampleTs: 0,        // epoch local de la última muestra recibida
    arrivalTimes: [],       // marcas de llegada para el Hz real medido
    lastSampleBySensor: {}, // sensor -> ts_ms (del mensaje)
    arrivalBySensor: {},    // sensor -> marcas de llegada (para el Hz por sensor)
    kpis: {},               // métricas -> historial corto para la tendencia
    status: null,           // último mensaje "status"
    vibRms: null,
    vibPeak: null,
    vibOver: false,
    inclTotal: null,        // norma del vector de inclinación (roll, pitch)
    lastAdxlArrival: 0,     // epoch local de la última muestra ADXL (prioridad de vibración)
  };

  // ───────────────────────────── referencias DOM ───────────────────────────

  const $ = (id) => document.getElementById(id);
  const els = {
    connDot: $("conn-dot"),
    connText: $("conn-text"),
    serialPort: $("serial-port"),
    utcClock: $("utc-clock"),
    windowSelect: $("window-select"),
    kpiHz: $("kpi-hz"), kpiHzTrend: $("kpi-hz-trend"), kpiHzSub: $("kpi-hz-sub"),
    kpiSamples: $("kpi-samples"), kpiSamplesTrend: $("kpi-samples-trend"),
    kpiTemp: $("kpi-temp"), kpiTempTrend: $("kpi-temp-trend"),
    kpiVib: $("kpi-vib"), kpiVibTrend: $("kpi-vib-trend"),
    kpiVibCard: $("kpi-vib-card"),
    kpiOrient: $("kpi-orient"), kpiOrientTrend: $("kpi-orient-trend"),
    kpiLatency: $("kpi-latency"), kpiLatencyTrend: $("kpi-latency-trend"),
    valRoll: $("val-roll"), valPitch: $("val-pitch"), valYaw: $("val-yaw"),
    valTemp: $("val-temp"), valAccelMag: $("val-accel-mag"), valGyroMag: $("val-gyro-mag"),
    vibThreshold: $("vib-threshold"),
    vibState: $("vib-state"),
    badgeMpu: $("badge-mpu"), badgeAdxl: $("badge-adxl"),
    events: $("events-console"), eventsClear: $("events-clear"),
    panelAdxl: $("panel-adxl"),
    // Modelo 3D de la torre: HUD de orientación y controles del panel.
    model3dRoll: $("model3d-roll"), model3dPitch: $("model3d-pitch"), model3dYaw: $("model3d-yaw"),
    model3dIncl: $("model3d-incl"), model3dInclBadge: $("model3d-incl-badge"),
    model3dThreshold: $("model3d-threshold"),
    model3dReset: $("model3d-reset"), model3dPause: $("model3d-pause"),
    model3dAxes: $("model3d-axes"), model3dShadow: $("model3d-shadow"),
    badgeModel3d: $("badge-model3d"),
    // HUD de vibración dentro del panel del modelo 3D.
    model3dVibRms: $("model3d-vib-rms"), model3dVibPeak: $("model3d-vib-peak"),
    model3dVibBar: $("model3d-vib-bar"), model3dVibThr: $("model3d-vib-thr"),
    model3dGainEcho: $("model3d-gain-echo"),
    // Configuración de la geometría de la torre.
    cfgForm: $("model3d-cfg-form"),
    cfgApply: $("model3d-cfg-apply"), cfgReset: $("model3d-cfg-reset"),
    cfgAntenna: $("cfg-antenna"),
  };

  // ───────────────────────────── gráficas ──────────────────────────────────

  const windowMs = () => Number(els.windowSelect.value) || 60000;

  const chartMpuAccel = new TimeSeriesChart({
    canvas: $("chart-mpu-accel"),
    legendEl: $("legend-mpu-accel"),
    title: "MPU6050 · aceleración X/Y/Z",
    yUnit: "m/s²",
    decimals: 2,
    windowMs,
    series: [
      { key: "accel_x", label: "accel_x", color: SERIES.x },
      { key: "accel_y", label: "accel_y", color: SERIES.y },
      { key: "accel_z", label: "accel_z", color: SERIES.z },
    ],
  });

  const chartMpuGyro = new TimeSeriesChart({
    canvas: $("chart-mpu-gyro"),
    legendEl: $("legend-mpu-gyro"),
    title: "MPU6050 · giroscopio X/Y/Z",
    yUnit: "°/s",
    decimals: 2,
    windowMs,
    series: [
      { key: "gyro_x", label: "gyro_x", color: SERIES.x },
      { key: "gyro_y", label: "gyro_y", color: SERIES.y },
      { key: "gyro_z", label: "gyro_z", color: SERIES.z },
    ],
  });

  const chartAdxlAccel = new TimeSeriesChart({
    canvas: $("chart-adxl-accel"),
    legendEl: $("legend-adxl-accel"),
    title: "ADXL345 · aceleración X/Y/Z",
    yUnit: "m/s²",
    decimals: 3,
    windowMs,
    series: [
      { key: "accel_x", label: "accel_x", color: SERIES.x },
      { key: "accel_y", label: "accel_y", color: SERIES.y },
      { key: "accel_z", label: "accel_z", color: SERIES.z },
    ],
  });

  // Gráfica clave de la tesis: vib_rms / vib_peak con umbral visual.
  const chartVib = new TimeSeriesChart({
    canvas: $("chart-vib"),
    legendEl: $("legend-vib"),
    title: "ADXL345 · vibración (RMS y pico)",
    yUnit: "m/s²",
    decimals: 3,
    windowMs,
    series: [
      { key: "vib_rms", label: "vib_rms", color: SERIES.rms },
      { key: "vib_peak", label: "vib_peak", color: SERIES.peak },
    ],
    thresholds: [{ key: "thr", label: "Umbral", color: STATUS.warn }],
  });

  const horizon = new InclinationWidget($("horizon"));

  const sparkHz = new Sparkline($("spark-hz"), SERIES.x);
  const sparkTemp = new Sparkline($("spark-temp"), SERIES.y);
  const sparkVib = new Sparkline($("spark-vib"), SERIES.rms);
  const sparkLatency = new Sparkline($("spark-latency"), SERIES.z);

  // ─────────────────────── modelo 3D de la torre ─────────────────────────
  // La orientación del modelo viene SOLO de derived.roll/pitch/yaw del
  // servidor (CONTRATO §5): aquí se traslada al modelo y a su HUD, nunca se
  // recalcula en el cliente. El suavizado visual (interpolación exponencial
  // hacia el último ángulo) vive en model3d.js, junto a la cámara.

  const model3d = window.TesisModel3D
    ? new window.TesisModel3D.TowerModel($("model3d-canvas"))
    : null;

  // Umbral visual de la inclinación total, con la misma mecánica de
  // persistencia que el umbral de vibración (localStorage, tolerante a su
  // ausencia en modo privado). Semáforo: verde por debajo de 0.6·T, ámbar
  // por debajo de T, rojo a partir de T. Es SOLO visual: no dispara alertas.
  function loadInclThreshold() {
    let saved = NaN;
    try { saved = Number(localStorage.getItem(INCL_THRESHOLD_KEY)); } catch (err) { /* sin persistencia */ }
    const value = Number.isFinite(saved) && saved > 0 ? saved : 5;
    els.model3dThreshold.value = String(value);
    return value;
  }

  let inclThresholdValue = loadInclThreshold();

  els.model3dThreshold.addEventListener("change", () => {
    const v = Number(els.model3dThreshold.value);
    if (!Number.isFinite(v) || v <= 0) {
      els.model3dThreshold.value = String(inclThresholdValue); // descarta inválidos
      return;
    }
    inclThresholdValue = v;
    try { localStorage.setItem(INCL_THRESHOLD_KEY, String(v)); } catch (err) { /* sin persistencia */ }
    updateInclState();
  });

  /** Semáforo del indicador de inclinación total. Color + icono + texto. */
  function updateInclState() {
    const incl = state.inclTotal;
    const badge = els.model3dInclBadge;
    if (!Number.isFinite(incl)) {
      badge.className = "badge badge-off";
      badge.textContent = "● SIN DATOS";
      return;
    }
    const t = inclThresholdValue;
    let cls, text;
    if (incl >= t) { cls = "incl-alarm"; text = "■ ALARMA"; }
    else if (incl >= 0.6 * t) { cls = "incl-warn"; text = "▲ PRECAUCIÓN"; }
    else { cls = "incl-ok"; text = "● NORMAL"; }
    badge.className = "badge " + cls;
    badge.textContent = text;
  }

  /** Traslada una orientación medida (grados) al modelo 3D y a su HUD. */
  function feedModel3D(roll, pitch, yaw) {
    if (Number.isFinite(roll)) els.model3dRoll.textContent = roll.toFixed(2) + "°";
    if (Number.isFinite(pitch)) els.model3dPitch.textContent = pitch.toFixed(2) + "°";
    if (Number.isFinite(yaw)) els.model3dYaw.textContent = yaw.toFixed(2) + "°";
    if (Number.isFinite(roll) && Number.isFinite(pitch)) {
      // Inclinación total: norma del vector de inclinación (roll, pitch).
      state.inclTotal = Math.sqrt(roll * roll + pitch * pitch);
      els.model3dIncl.textContent = state.inclTotal.toFixed(2) + "°";
      updateInclState();
    }
    // El modelo solo recibe el objetivo: se rota al ritmo de los datos sin
    // acumular atraso (ver suavizado en model3d.js).
    if (model3d) model3d.setOrientation(roll, pitch, yaw);
  }

  /**
   * Traslada una vibración medida al modelo 3D y a su HUD.
   *
   * El balanceo de la celosía es la respuesta visual a derived.vib_rms; el
   * halo del punto de montaje sigue a derived.vib_peak; la dirección del
   * balanceo sale del residual de la aceleración cruda (raw.accel_*), todo
   * según VISUALIZACION_3D.md. La barra de nivel usa el MISMO umbral visual
   * que la gráfica de vibración, para que el modelo y el gráfico cuenten la
   * misma historia con la misma escala.
   *
   * El modelo 3D recibe CADA muestra (50 Hz: el suavizado vive en model3d.js
   * y un objetivo por mensaje no cuesta casi nada), pero el DOM del HUD se
   * actualiza como mucho a 20 Hz: leer/escribir textContent y style 100 veces
   * por segundo no aporta información que el ojo pueda seguir y sí presiona
   * el presupuesto de frame del contrato §6.
   */
  const MODEL3D_HUD_MS = 50;
  let lastModel3dHudMs = 0;

  function feedModel3DVib(rms, peak, ax, ay, az) {
    if (model3d) model3d.setVibration(rms, peak, ax, ay, az);
    const now = Date.now();
    if (now - lastModel3dHudMs < MODEL3D_HUD_MS) return;
    lastModel3dHudMs = now;
    if (Number.isFinite(rms)) {
      els.model3dVibRms.textContent = rms.toFixed(3) + " m/s²";
    }
    if (Number.isFinite(peak)) {
      els.model3dVibPeak.textContent = peak.toFixed(3) + " m/s²";
    }
    updateVibBar(rms);
  }

  /** Barra de nivel de vibración frente al umbral visual (color + ancho). */
  function updateVibBar(rms) {
    const bar = els.model3dVibBar;
    if (!bar) return;
    // Sin flujo reciente el nivel no es "el último valor", es NINGUNO: la
    // barra se apaga para que no se lea como una medición vigente.
    const stale = !state.lastSampleTs || Date.now() - state.lastSampleTs >= MODEL3D_STALE_MS;
    if (stale) {
      bar.style.width = "0%";
      bar.className = "vib-bar-fill";
      return;
    }
    const level = Number.isFinite(rms) ? rms : state.vibRms;
    const t = thresholdValue;
    if (!Number.isFinite(level) || !Number.isFinite(t) || t <= 0) {
      bar.style.width = "0%";
      bar.className = "vib-bar-fill";
      return;
    }
    // Escala relativa al umbral: 100 % = justo en el umbral. Se permite
    // pasar del 100 % para que el rojo se lea como "por encima".
    bar.style.width = Math.min(100, (level / t) * 100).toFixed(1) + "%";
    if (level >= t) bar.className = "vib-bar-fill incl-alarm";
    else if (level >= 0.6 * t) bar.className = "vib-bar-fill incl-warn";
    else bar.className = "vib-bar-fill";
  }

  const chartsByTable = {
    "mpu-accel": chartMpuAccel,
    "mpu-gyro": chartMpuGyro,
    "adxl-accel": chartAdxlAccel,
    "vib": chartVib,
  };

  // ─────────────────────────── umbral de vibración ─────────────────────────
  // El umbral es SOLO visual (marca la línea y colorea la tarjeta). La lógica
  // de alertas llegará en la fase de detección de anomalías. Se persiste en
  // localStorage para que sobreviva a un refresco en el puesto de vigilancia.

  function loadThreshold() {
    // localStorage puede no estar disponible (modo privado, file://): en ese
    // caso se usa el valor por defecto sin romper el arranque.
    let saved = NaN;
    try { saved = Number(localStorage.getItem(VIB_THRESHOLD_KEY)); } catch (err) { /* sin persistencia */ }
    const value = Number.isFinite(saved) && saved > 0 ? saved : 0.5;
    els.vibThreshold.value = String(value);
    chartVib.setThreshold("thr", value);
    // El HUD del modelo 3D muestra el mismo umbral que la gráfica: una sola
    // referencia visual en toda la página.
    if (els.model3dVibThr) els.model3dVibThr.textContent = value.toFixed(2);
    return value;
  }

  let thresholdValue = loadThreshold();

  els.vibThreshold.addEventListener("change", () => {
    const v = Number(els.vibThreshold.value);
    if (!Number.isFinite(v) || v < 0) {
      els.vibThreshold.value = String(thresholdValue); // descarta entradas inválidas
      return;
    }
    thresholdValue = v;
    try { localStorage.setItem(VIB_THRESHOLD_KEY, String(v)); } catch (err) { /* sin persistencia */ }
    chartVib.setThreshold("thr", v);
    if (els.model3dVibThr) els.model3dVibThr.textContent = v.toFixed(2);
    updateVibState();
    updateVibBar();
  });

  function updateVibState() {
    // La magnitud de umbral del contrato es vib_rms (sección 5).
    const over = Number.isFinite(state.vibRms) && state.vibRms > thresholdValue;
    state.vibOver = over;
    els.kpiVibCard.classList.toggle("kpi-alert", over);
    els.vibState.hidden = !over;
    if (over) {
      // Estado = color + icono + texto: nunca solo color.
      els.vibState.textContent = "▲ SOBRE UMBRAL";
      els.vibState.classList.add("badge-alert");
    } else {
      els.vibState.textContent = "";
      els.vibState.classList.remove("badge-alert");
    }
  }

  // ───────────────────────── consola de eventos ────────────────────────────
  // Los mensajes "log" del WebSocket (y los errores del propio cliente) se
  // pintan con textContent: el contenido viene del puerto serie y no es de
  // confianza para inserción HTML.

  function appendLog(tsMs, level, sensor, line) {
    const row = document.createElement("div");
    row.className = `log-row log-${level || "info"}`;
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = formatTimeUTC(tsMs || Date.now(), true);
    const badge = document.createElement("span");
    badge.className = "log-level";
    badge.textContent = (level || "info").toUpperCase();
    const tag = document.createElement("span");
    tag.className = "log-sensor";
    tag.textContent = sensor || "sistema";
    const msg = document.createElement("span");
    msg.className = "log-msg";
    msg.textContent = line || "";
    row.append(time, badge, tag, msg);

    // Auto-scroll solo si el usuario no ha subido a revisar el histórico.
    const nearBottom = els.events.scrollHeight - els.events.scrollTop - els.events.clientHeight < 40;
    els.events.appendChild(row);
    while (els.events.childElementCount > EVENTS_MAX) {
      els.events.removeChild(els.events.firstElementChild);
    }
    if (nearBottom) els.events.scrollTop = els.events.scrollHeight;
  }

  els.eventsClear.addEventListener("click", () => {
    els.events.textContent = "";
  });

  // ─────────────────────────── WebSocket + REST ────────────────────────────

  function wsUrl() {
    // Mismo origen que la página: sirve tanto en 127.0.0.1 como en la LAN.
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws`;
  }

  function connect() {
    let ws;
    try {
      ws = new WebSocket(wsUrl());
    } catch (err) {
      appendLog(Date.now(), "error", null, `No se pudo crear el WebSocket: ${err.message}`);
      scheduleReconnect();
      return;
    }
    state.ws = ws;

    ws.addEventListener("open", () => {
      state.wsOpen = true;
      state.reconnectAttempts = 0;
      updateConnIndicator();
      appendLog(Date.now(), "info", null, "WebSocket conectado a " + wsUrl());
      // Tras cada (re)conexión se rellena el histórico: la gráfica no debe
      // arrancar vacía justo después de una caída.
      backfillHistory();
    });

    ws.addEventListener("message", (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (err) {
        appendLog(Date.now(), "warn", null, "Mensaje WebSocket no es JSON válido (descartado)");
        return;
      }
      switch (msg.type) {
        case "sample": handleSample(msg); break;
        case "status": handleStatus(msg); break;
        case "log": handleLog(msg); break;
        default:
          // Tipos desconocidos se ignoran a propósito: el contrato puede
          // crecer y una versión antigua del dashboard no debe romperse.
          break;
      }
    });

    ws.addEventListener("close", () => {
      state.wsOpen = false;
      updateConnIndicator();
      scheduleReconnect();
    });

    ws.addEventListener("error", () => {
      // "close" siempre acompaña a "error": ahí se gestiona la reconexión.
      updateConnIndicator();
    });
  }

  function scheduleReconnect() {
    state.reconnectAttempts++;
    // Retroceso exponencial con techo: no se martillea al servidor y la
    // recuperación tras un reinicio del ESP32 es automática.
    const delay = Math.min(10000, 500 * Math.pow(2, Math.min(state.reconnectAttempts, 5)));
    setTimeout(connect, delay + Math.random() * 250);
  }

  /**
   * GET /api/history — rellena las gráficas con la última ventana.
   * Se consume con tolerancia: si el servidor no devuelve una serie, esa
   * gráfica sencillamente empieza vacía y se llena con el flujo en vivo.
   */
  async function backfillHistory() {
    for (const sensor of ["mpu6050", "adxl345"]) {
      try {
        // Defensa explícita (el defecto del contrato §4.1 ya es metrics="todos"):
        // se piden por nombre las métricas que dibuja este dashboard para que
        // una poda futura del servidor no deje gráficas vacías sin aviso.
        const metrics =
          sensor === "mpu6050"
            ? "accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z,temp_c"
            : "accel_x,accel_y,accel_z,vib_rms,vib_peak";
        const res = await fetch(
          `/api/history?sensor=${sensor}&seconds=60&limit=3000&metrics=${metrics}`,
          {
            headers: { Accept: "application/json" },
          }
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const series = data && data.series ? data.series : {};
        const asPoints = (key) => (Array.isArray(series[key]) ? series[key] : []);

        if (sensor === "mpu6050") {
          for (const k of ["accel_x", "accel_y", "accel_z"]) chartMpuAccel.loadHistory(k, asPoints(k));
          // El histórico guarda rad/s igual que la BD: se convierte al cargar.
          for (const k of ["gyro_x", "gyro_y", "gyro_z"]) {
            chartMpuGyro.loadHistory(k, asPoints(k).map(([t, v]) => [t, v * RAD_TO_DEG]));
          }
        } else {
          for (const k of ["accel_x", "accel_y", "accel_z"]) chartAdxlAccel.loadHistory(k, asPoints(k));
          for (const k of ["vib_rms", "vib_peak"]) chartVib.loadHistory(k, asPoints(k));
        }
        const count = data && Number.isFinite(data.count) ? data.count : "?";
        appendLog(Date.now(), "info", sensor, `Histórico cargado: ${count} puntos (60 s)`);
      } catch (err) {
        appendLog(Date.now(), "warn", sensor, `Histórico no disponible: ${err.message}`);
      }
    }
  }

  // ─────────────────────────── mensajes del WS ─────────────────────────────

  /** "sample" (contrato 4.2): raw.* y derived.* según el sensor. */
  function handleSample(msg) {
    const ts = Number(msg.ts_ms);
    if (!Number.isFinite(ts)) return;
    const raw = msg.raw || {};
    const derived = msg.derived || {};
    const sensor = msg.sensor;

    state.lastSampleTs = Date.now();
    state.lastSampleBySensor[sensor] = ts;
    noteArrival(sensor);
    noteKpi("latency", Date.now() - ts);

    if (sensor === "mpu6050") {
      const fx = Number(raw.accel_x), fy = Number(raw.accel_y), fz = Number(raw.accel_z);
      if (Number.isFinite(fx)) chartMpuAccel.push("accel_x", ts, fx);
      if (Number.isFinite(fy)) chartMpuAccel.push("accel_y", ts, fy);
      if (Number.isFinite(fz)) chartMpuAccel.push("accel_z", ts, fz);

      // Conversión rad/s -> °/s solo para presentar (ver cabecera del archivo).
      const gx = Number(raw.gyro_x), gy = Number(raw.gyro_y), gz = Number(raw.gyro_z);
      if (Number.isFinite(gx)) chartMpuGyro.push("gyro_x", ts, gx * RAD_TO_DEG);
      if (Number.isFinite(gy)) chartMpuGyro.push("gyro_y", ts, gy * RAD_TO_DEG);
      if (Number.isFinite(gz)) chartMpuGyro.push("gyro_z", ts, gz * RAD_TO_DEG);

      const temp = Number(raw.temp_c);
      if (Number.isFinite(temp)) {
        els.valTemp.textContent = temp.toFixed(2) + " °C";
        noteKpi("temp", temp);
        sparkTemp.push(ts, temp);
      }

      // Orientación: el contrato la entrega ya en grados.
      const roll = Number(derived.roll), pitch = Number(derived.pitch), yaw = Number(derived.yaw);
      if (Number.isFinite(roll)) els.valRoll.textContent = roll.toFixed(2) + "°";
      if (Number.isFinite(pitch)) els.valPitch.textContent = pitch.toFixed(2) + "°";
      if (Number.isFinite(yaw)) els.valYaw.textContent = yaw.toFixed(2) + "°";
      if (Number.isFinite(roll) && Number.isFinite(pitch)) {
        horizon.setOrientation(roll, pitch);
        noteKpi("orient", Math.max(Math.abs(roll), Math.abs(pitch)));
      }

      // Modelo 3D: la torre se inclina con esta misma orientación derivada.
      feedModel3D(roll, pitch, yaw);

      const am = Number(derived.accel_mag), gm = Number(derived.gyro_mag);
      if (Number.isFinite(am)) els.valAccelMag.textContent = am.toFixed(3) + " m/s²";
      if (Number.isFinite(gm)) els.valGyroMag.textContent = gm.toFixed(5) + " rad/s";

      // El MPU6050 también emite vib_rms/vib_peak según el contrato: son el
      // respaldo del KPI y del modelo 3D si el ADXL345 no estuviera presente
      // (o si dejara de emitir en caliente).
      const adxlSilent = Date.now() - state.lastAdxlArrival > 2000;
      if (adxlSilent && Number.isFinite(derived.vib_rms)) {
        state.vibRms = Number(derived.vib_rms);
        if (Number.isFinite(derived.vib_peak)) state.vibPeak = Number(derived.vib_peak);
        updateVibState();
        feedModel3DVib(state.vibRms, state.vibPeak, fx, fy, fz);
      }
    } else if (sensor === "adxl345") {
      const fx = Number(raw.accel_x), fy = Number(raw.accel_y), fz = Number(raw.accel_z);
      if (Number.isFinite(fx)) chartAdxlAccel.push("accel_x", ts, fx);
      if (Number.isFinite(fy)) chartAdxlAccel.push("accel_y", ts, fy);
      if (Number.isFinite(fz)) chartAdxlAccel.push("accel_z", ts, fz);
      state.lastAdxlArrival = Date.now();

      const rms = Number(derived.vib_rms), peak = Number(derived.vib_peak);
      if (Number.isFinite(rms)) {
        chartVib.push("vib_rms", ts, rms);
        state.vibRms = rms;
        sparkVib.push(ts, rms);
        noteKpi("vib", rms);
      }
      if (Number.isFinite(peak)) {
        chartVib.push("vib_peak", ts, peak);
        state.vibPeak = peak;
      }
      // Vibración → modelo 3D: balanceo de la celosía + halo del sensor.
      feedModel3DVib(rms, peak, fx, fy, fz);
      if (Number.isFinite(rms)) updateVibState();
    } else {
      // Sensor no contemplado en este dashboard: se ignora sin error, el
      // contrato admite futuros sensores (HX711, DS18B20, ...).
    }
  }

  /** "status" (contrato 4.2): conexión del puerto serie y estado por sensor. */
  function handleStatus(msg) {
    state.status = msg;
    els.serialPort.textContent = msg.port || "—";

    const sensors = msg.sensors || {};
    let total = 0;
    for (const name of Object.keys(sensors)) {
      const s = sensors[name] || {};
      if (Number.isFinite(s.n_samples)) total += s.n_samples;
    }
    if (total > 0) noteKpi("samples", total);
    els.kpiSamples.textContent = total > 0 ? total.toLocaleString("es") : "—";

    const badgeFor = (el, info) => {
      if (!el) return;
      const present = info && info.present;
      el.textContent = present ? "presente" : "ausente";
      el.classList.toggle("badge-ok", !!present);
      el.classList.toggle("badge-off", !present);
    };
    badgeFor(els.badgeMpu, sensors.mpu6050);
    badgeFor(els.badgeAdxl, sensors.adxl345);

    updateConnIndicator();
  }

  /** "log" (contrato 4.2): evento legible con nivel info/warn/error. */
  function handleLog(msg) {
    appendLog(Number(msg.ts_ms) || Date.now(), msg.level, msg.sensor, msg.line);
  }

  // ─────────────────────────────── KPIs ────────────────────────────────────

  function noteKpi(key, value) {
    if (!Number.isFinite(value)) return;
    const arr = state.kpis[key] || (state.kpis[key] = []);
    arr.push({ t: Date.now(), v: value });
    // Solo hace falta la última década de segundos para calcular la tendencia.
    while (arr.length > 200 || (arr.length && arr[0].t < Date.now() - 15000)) arr.shift();
  }

  /** Delta contra el valor de hace ~5 s (la "tendencia" de cada tarjeta). */
  function trendOf(key) {
    const arr = state.kpis[key];
    if (!arr || arr.length < 2) return null;
    const now = arr[arr.length - 1];
    const past = arr.find((p) => p.t <= now.t - 5000) || arr[0];
    return now.v - past.v;
  }

  function renderTrend(el, key, decimals, unit) {
    const d = trendOf(key);
    if (d === null) { el.textContent = "—"; return; }
    const arrow = Math.abs(d) < 1e-9 ? "→" : d > 0 ? "▲" : "▼";
    el.textContent = `${arrow} ${d > 0 ? "+" : ""}${d.toFixed(decimals)}${unit ? " " + unit : ""} / 5 s`;
  }

  function noteArrival(sensor) {
    const now = Date.now();
    state.arrivalTimes.push(now);
    while (state.arrivalTimes.length && state.arrivalTimes[0] < now - 2000) state.arrivalTimes.shift();
    // Conteo por sensor para el Hz desglosado de la tarjeta de flujo.
    const per = (state.arrivalBySensor[sensor] || (state.arrivalBySensor[sensor] = []));
    per.push(now);
    while (per.length && per[0] < now - 2000) per.shift();
  }

  /** Hz real medido: muestras recibidas en el último segundo (contrato 6). */
  function measuredHz() {
    const now = Date.now();
    let n = 0;
    for (const t of state.arrivalTimes) if (t >= now - 1000) n++;
    return n;
  }

  function updateKpis() {
    // 0. Modelo 3D: si no llegan muestras el modelo queda quieto y el panel
    //    muestra el estado "sin datos" sin romper la UI.
    if (model3d) {
      const fresh = !!state.lastSampleTs && Date.now() - state.lastSampleTs < MODEL3D_STALE_MS;
      model3d.setHasData(fresh);
      els.badgeModel3d.textContent = fresh ? "en vivo" : "sin datos";
      els.badgeModel3d.classList.toggle("badge-ok", fresh);
      els.badgeModel3d.classList.toggle("badge-off", !fresh);
      if (!fresh) {
        // El canvas dice "Sin datos": el HUD no puede seguir mostrando las
        // últimas lecturas como si fueran vigentes. Se vacía al mismo estado
        // inicial y la barra de nivel se apaga (ver updateVibBar).
        for (const el of [els.model3dRoll, els.model3dPitch, els.model3dYaw,
          els.model3dIncl, els.model3dVibRms, els.model3dVibPeak]) {
          el.textContent = "—";
        }
        els.model3dInclBadge.className = "badge badge-off";
        els.model3dInclBadge.textContent = "● SIN DATOS";
      }
    }

    // 1. Flujo: Hz reales medidos en el navegador (no los nominales).
    const hz = state.lastSampleTs ? measuredHz() : 0;
    els.kpiHz.textContent = state.lastSampleTs ? hz.toFixed(0) : "—";
    noteKpi("hz", hz);
    renderTrend(els.kpiHzTrend, "hz", 0, "Hz");
    const hzAdxl = hzForSensor("adxl345");
    const hzMpu = hzForSensor("mpu6050");
    els.kpiHzSub.textContent =
      `nominal 50 Hz · mpu6050: ${hzMpu !== null ? hzMpu + " Hz" : "—"} · adxl345: ${hzAdxl !== null ? hzAdxl + " Hz" : "—"}`;
    sparkHz.push(Date.now(), hz);

    // 2. Muestras totales ya se pinta en handleStatus; tendencia aquí.
    renderTrend(els.kpiSamplesTrend, "samples", 0, "muestras");

    // 3. Temperatura del MPU6050.
    const temp = state.kpis.temp ? state.kpis.temp[state.kpis.temp.length - 1].v : null;
    els.kpiTemp.textContent = temp !== null ? temp.toFixed(2) : "—";
    renderTrend(els.kpiTempTrend, "temp", 2, "°C");

    // 4. Vibración RMS actual + estado frente al umbral visual.
    els.kpiVib.textContent = Number.isFinite(state.vibRms) ? state.vibRms.toFixed(3) : "—";
    renderTrend(els.kpiVibTrend, "vib", 3, "m/s²");
    updateVibState();
    updateVibBar();

    // 5. Pitch/Roll actuales (la tarjeta muestra el par; la tendencia usa la
    //    inclinación máxima como magnitud comparable).
    const roll = els.valRoll.textContent, pitch = els.valPitch.textContent;
    els.kpiOrient.textContent = (pitch !== "—" && roll !== "—")
      ? `P ${pitch} / R ${roll}` : "—";
    renderTrend(els.kpiOrientTrend, "orient", 2, "°");

    // 6. Latencia de ingesta: ts_ms del servidor frente al reloj local.
    const lat = state.kpis.latency ? state.kpis.latency[state.kpis.latency.length - 1].v : null;
    els.kpiLatency.textContent = lat !== null ? Math.max(0, lat).toFixed(0) : "—";
    renderTrend(els.kpiLatencyTrend, "latency", 0, "ms");
    if (lat !== null) sparkLatency.push(Date.now(), Math.max(0, lat));
  }

  function hzForSensor(sensor) {
    // Hz = llegadas de ese sensor en el último segundo (mismo criterio que
    // el Hz global, pero sobre la cola de llegadas propia del sensor).
    const arr = state.arrivalBySensor[sensor];
    if (!arr || !arr.length) return null;
    const now = Date.now();
    return arr.filter((t) => t >= now - 1000).length;
  }

  // ───────────────────────── indicador de conexión ─────────────────────────
  // Verde: WebSocket abierto y llegando muestras (< 2 s). Ámbar: abierto pero
  // sin flujo (o el servidor marca connected=false). Rojo: sin WebSocket.

  function updateConnIndicator() {
    const age = state.lastSampleTs ? Date.now() - state.lastSampleTs : Infinity;
    const serialOk = state.status ? state.status.connected === true : false;
    let cls, text;
    if (!state.wsOpen) {
      cls = "conn-red"; text = "SIN CONEXIÓN — reconectando…";
    } else if (age < 2000 && (serialOk || state.status === null)) {
      cls = "conn-green"; text = "EN VIVO";
    } else {
      cls = "conn-amber"; text = serialOk ? "ESPERANDO DATOS" : "PUERTO SERIE INACTIVO";
    }
    els.connDot.className = "conn-dot " + cls;
    els.connText.className = cls;
    els.connText.textContent = text;
  }

  // ──────────────────────────── bucles de UI ───────────────────────────────

  setInterval(updateKpis, 1000);          // KPIs y tendencias: 1 Hz basta
  setInterval(updateConnIndicator, 1000); // el estado envejece aunque no haya mensajes
  setInterval(() => {
    els.utcClock.textContent = formatTimeUTC(Date.now(), false);
  }, 250);

  // ────────────────────── vistas de tabla por gráfica ──────────────────────
  // Gemelo accesible de cada gráfica: ningún valor depende del tooltip.

  document.querySelectorAll("[data-table]").forEach((btn) => {
    const key = btn.getAttribute("data-table");
    const wrap = $("table-" + key);
    const thead = wrap.querySelector("thead");
    const tbody = wrap.querySelector("tbody");
    btn.addEventListener("click", () => {
      const open = wrap.hidden;
      wrap.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
      btn.textContent = open ? "Ocultar tabla" : "Ver tabla";
      if (open) chartsByTable[key].buildTable(thead, tbody, 30);
    });
  });

  // Al cambiar la ventana temporal, una sola fila de controles arriba gobierna
  // todas las gráficas: los números de todas las tarjetas siguen coincidiendo.
  els.windowSelect.addEventListener("change", () => {
    for (const c of [chartMpuAccel, chartMpuGyro, chartAdxlAccel, chartVib]) {
      c.clear(); // la ventana cambia de escala: se recarga el histórico limpio
    }
    backfillHistory();
  });

  // ─────────────────────── controles del modelo 3D ───────────────────────
  // "Restablecer vista" solo restaura la cámara: la orientación del modelo
  // la sigue fijando el sensor. "Pausar" congela el modelo (la cámara y los
  // lectores de datos siguen vivos) y al reanudar se retoma el último ángulo
  // medido sin salto brusco.

  els.model3dReset.addEventListener("click", () => {
    if (model3d) model3d.resetView();
  });

  let model3dPaused = false;
  els.model3dPause.addEventListener("click", () => {
    model3dPaused = !model3dPaused;
    if (model3d) model3d.setPaused(model3dPaused);
    els.model3dPause.textContent = model3dPaused ? "Reanudar" : "Pausar";
  });

  els.model3dAxes.addEventListener("change", () => {
    if (model3d) model3d.setShowAxes(els.model3dAxes.checked);
  });

  els.model3dShadow.addEventListener("change", () => {
    if (model3d) model3d.setShowShadow(els.model3dShadow.checked);
  });

  // ─────────── configuración de la geometría del modelo 3D ───────────────
  // La torre y la antena del modelo se parametrizan para que cada prueba de
  // campo pueda reproducir la estructura real sin tocar código. Los valores
  // por defecto son los de la torre de referencia de la tesis (los define
  // model3d.js en DEFAULT_CFG: una sola fuente de verdad). Se persisten en
  // localStorage y solo cambian la GEOMETRÍA dibujada: ningún dato medido se
  // toca (VISUALIZACION_3D.md).

  const cfgInputs = {
    height: $("cfg-height"), baseWidth: $("cfg-base"), topWidth: $("cfg-top"),
    segments: $("cfg-segments"), mastHeight: $("cfg-mast"),
    panelW: $("cfg-panel-w"), panelH: $("cfg-panel-h"), panelD: $("cfg-panel-d"),
    dishD: $("cfg-dish-d"), omniL: $("cfg-omni-l"), omniR: $("cfg-omni-r"),
    sensorPos: $("cfg-sensor-pos"), swayGain: $("cfg-sway-gain"),
  };

  /** Lee el formulario en unidades del modelo (sensorPos como fracción). */
  function readCfgForm() {
    const raw = { antenna: els.cfgAntenna.value };
    for (const key of Object.keys(cfgInputs)) {
      let v = Number(cfgInputs[key].value);
      if (key === "sensorPos") v = v / 100; // el formulario va en %
      raw[key] = v;
    }
    return raw;
  }

  /** Escribe la configuración (ya saneada) en el formulario. */
  function writeCfgForm(cfg) {
    for (const key of Object.keys(cfgInputs)) {
      const v = key === "sensorPos" ? Math.round(cfg[key] * 100) : cfg[key];
      cfgInputs[key].value = String(v);
    }
    els.cfgAntenna.value = cfg.antenna;
    updateAntennaFields();
    if (els.model3dGainEcho) els.model3dGainEcho.textContent = Number(cfg.swayGain).toFixed(2);
  }

  /** Habilita/deshabilita los campos según el tipo de antena elegido. */
  function updateAntennaFields() {
    const type = els.cfgAntenna.value;
    const wants = {
      panel: type === "panel" || type === "panel+disco",
      disco: type === "disco" || type === "panel+disco",
      omni: type === "omni",
    };
    document.querySelectorAll("#model3d-cfg-form .field[data-antenna]").forEach((field) => {
      const group = field.getAttribute("data-antenna");
      const on = !!wants[group];
      // La clase da el aspecto; `disabled` es lo que retira el campo del
      // orden de tabulación y lo anuncia como no disponible a los lectores
      // de pantalla (la clase sola deja el campo editable por teclado).
      field.classList.toggle("is-disabled", !on);
      field.querySelectorAll("input, select").forEach((input) => {
        input.disabled = !on;
      });
    });
  }

  function saveCfg(cfg) {
    try { localStorage.setItem(MODEL3D_CFG_KEY, JSON.stringify(cfg)); } catch (err) { /* sin persistencia */ }
  }

  /** Restaurar predeterminados: la torre de referencia de la tesis. */
  function applyDefaultCfg() {
    const def = window.TesisModel3D ? window.TesisModel3D.DEFAULT_CFG : null;
    if (!def) return;
    const applied = model3d ? model3d.setConfig(def) : def;
    writeCfgForm(applied);
    try { localStorage.removeItem(MODEL3D_CFG_KEY); } catch (err) { /* sin persistencia */ }
    appendLog(Date.now(), "info", "modelo3d", "Geometría restaurada a los valores predeterminados");
  }

  /** Arranque: config guardada en este navegador, o predeterminados. */
  function initCfg() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(MODEL3D_CFG_KEY) || "null"); } catch (err) { /* datos corruptos */ }
    const def = window.TesisModel3D ? window.TesisModel3D.DEFAULT_CFG : null;
    if (!def) { updateAntennaFields(); return; }
    // sanitizeCfg completa huecos con los predeterminados: un localStorage
    // antiguo o a medias no deja el modelo a medio construir.
    const wanted = window.TesisModel3D.sanitizeCfg(saved || def);
    const applied = model3d ? model3d.setConfig(wanted) : wanted;
    writeCfgForm(applied);
    if (saved) saveCfg(applied); // normaliza lo persistido
    updateAntennaFields();
  }

  els.cfgApply.addEventListener("click", () => {
    if (!model3d) return;
    const applied = model3d.setConfig(readCfgForm());
    writeCfgForm(applied); // refleja los recortes de rango aplicados
    saveCfg(applied);
    appendLog(Date.now(), "info", "modelo3d", `Geometría aplicada: ${applied.height} m de celosía, antena ${applied.antenna}`);
  });

  els.cfgReset.addEventListener("click", applyDefaultCfg);
  els.cfgAntenna.addEventListener("change", updateAntennaFields);
  // Enter en un campo numérico no debe enviar el formulario (recargaría la página).
  els.cfgForm.addEventListener("submit", (ev) => ev.preventDefault());

  initCfg();

  // ────────────────────────────── arranque ────────────────────────────────

  appendLog(Date.now(), "info", null, "Dashboard iniciado — esperando datos del servidor");
  updateKpis();
  updateConnIndicator();
  connect();
})();
