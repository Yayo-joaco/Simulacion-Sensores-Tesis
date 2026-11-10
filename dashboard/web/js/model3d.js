/*
 * model3d.js — Torre de celosía 3D en tiempo real sobre <canvas> 2D.
 *
 * ¿Por qué un renderizador propio? El requisito del sistema es funcionar sin
 * Internet en el tramo rural (ARQUITECTURA.md 2.5): ningún CDN, ningún
 * Three.js, ningún build. La proyección en perspectiva, la rotación de
 * vértices y el ordenado por profundidad son pocas decenas de líneas y
 * quedan documentadas aquí como parte del capítulo de visualización de la
 * tesis. Canvas 2D da suficiente rendimiento para ~1100 polígonos a 60 fps.
 *
 * Convención de ejes — la MISMA que el resto del proyecto (CONTRATO §5 y
 * processing/README.md):
 *   +X derecha, +Y arriba, +Z hacia el espectador.
 *   R = Rz(yaw) · Ry(pitch) · Rx(roll), con roll/pitch/yaw en GRADOS tal
 *   como llegan en derived.roll/pitch/yaw del mensaje "sample".
 *
 * Equivalencia con el visualizador de Processing (MPU6050_Visualizer.pde):
 * el mapeo sensor→pantalla de Processing es un REFLEJO, por lo que su
 * drawScene aplica rotateZ con INVERT_YAW y rotateX con INVERT_ROLL
 * (ambos flags = true). Este módulo replica exactamente ese ajuste:
 * yawEff = -yaw y rollEff = -roll antes de componer R. Ambos visores se
 * mueven por tanto de forma idéntica ante la misma muestra.
 *
 * DOS fuentes independientes de movimiento sobre la misma estructura:
 *
 *   1. ORIENTACIÓN (MPU6050, derived.roll/pitch/yaw): la torre entera se
 *      inclina como cuerpo rígido. Es la inclinación lenta/persistente.
 *   2. VIBRACIÓN (ADXL345, derived.vib_rms/vib_peak): la celosía BALANCEA
 *      con una forma aproximada de primer modo: parábola (y/H)² con la base
 *      fija (exacta bajo momento flector constante; muy próxima al primer
 *      modo real del voladizo, cuyos dos primeros términos son 1.875²·y²/2).
 *      Es la respuesta dinámica rápida. La AMPLITUD del
 *      balanceo es proporcional a vib_rms medida (ganancia visual G en
 *      metros por m/s², configurable); la DIRECCIÓN sigue el movimiento
 *      medido en el plano horizontal (residual de aceleración tras retirar
 *      la gravedad con un EMA); la FRECUENCIA del balanceo es ILUSTRATIVA
 *      y fija (SWAY_HZ): lo que comunica el modelo es amplitud e intensidad,
 *      no la frecuencia modal real. Esta exageración de amplitud es la
 *      convención habitual en animaciones de dinámica estructural.
 *
 * Estructura del render por frame (painter's algorithm):
 *   1. Se compone la matriz de rotación 3x3 de la orientación medida.
 *   2. Cada vértice estático se rota, se le aplica la deflexión de balanceo
 *      (solo desplazamiento horizontal, proporcional a (y/H)²) y se
 *      transforma a espacio de cámara.
 *   3. Se proyecta en perspectiva a coordenadas de pantalla.
 *   4. Las caras se ordenan por profundidad del centroide y se pintan de
 *      lejos a cerca, con sombreado por normal y niebla suave por distancia.
 *   5. Se dibuja el halo pulsátil del punto de montaje del sensor, cuya
 *      intensidad sigue a vib_peak.
 * La geometría (vértices e índices de cara) se precalcula UNA vez al
 * construir o al cambiar la configuración (buildTower); el bucle de dibujo
 * solo rota, desplaza, proyecta y rellena, y recicla todos sus arrays
 * (sin asignaciones masivas por frame, criterio del contrato §6: 60 fps).
 */

(function (global) {
  "use strict";

  const DEG_TO_RAD = Math.PI / 180;

  // Ajuste de signo idéntico al del visor de Processing (INVERT_ROLL /
  // INVERT_YAW en MPU6050_Visualizer.pde). Ver cabecera de este archivo.
  const INVERT_ROLL = true;
  const INVERT_YAW = true;

  // Suavizado visual: interpolación exponencial por frame hacia el ángulo
  // recibido. Factor alto para que el modelo siga a los datos sin vibrar
  // pero SIN acumular atraso: solo se guarda el último objetivo, nunca una
  // cola de muestras pendientes.
  const SMOOTH = 0.35;

  // Suavizado de la amplitud de balanceo: más lento que el de orientación
  // para que un pico aislado no sacuda el modelo de golpe.
  const SWAY_SMOOTH = 0.18;

  // Frecuencia ILUSTRATIVA del balanceo (Hz). Fija a propósito: el modelo
  // comunica amplitud e intensidad de vibración, no frecuencia modal.
  const SWAY_HZ = 1.6;

  // Techo de la deflexión visual, como fracción de la altura de la celosía:
  // un 12 % de H llega a la cima sin volverse absurdo. Se documenta en la
  // etiqueta "ganancia visual" del HUD para que el analista sepa que la
  // amplitud está exagerada.
  const SWAY_MAX_FRAC = 0.12;

  // EMA de la gravedad estimada por eje para extraer el residual dinámico
  // (dirección del balanceo). Con muestras a 50 Hz, α = 0.02 da τ ≈ 1 s.
  const GRAVITY_EMA = 0.02;
  // Suavizado de la dirección del balanceo: evita que el balanceo cambie de
  // sentido por ruido cuando la amplitud es pequeña.
  const DIR_SMOOTH = 0.10;

  // Foco direccional fijo en espacio del mundo: la luz no depende de la
  // cámara, de modo que al orbitar el sombreado cambia de forma coherente.
  // Vector hacia la luz normalizado (desde la escena hacia la fuente).
  const LIGHT = (function () {
    const x = 0.45, y = 0.85, z = 0.55;
    const n = Math.sqrt(x * x + y * y + z * z);
    return [x / n, y / n, z / n];
  })();

  // Ambiente del sombreado: ninguna cara cae a negro puro. Valor calibrado
  // a ojo contra el fondo #121b2e del panel: la celosía debe leerse entera.
  const AMBIENT = 0.38;

  // Fondo del panel (coincide con la superficie #121b2e del dashboard) hacia
  // el que se mezclan las caras lejanas (niebla suave).
  const FOG_RGB = [18, 27, 46];

  // Paleta de la estructura: acero gris azulado con acentos cian/violeta.
  const RGB = {
    leg: [134, 154, 184],     // patas: acero gris azulado
    brace: [116, 136, 166],   // anillos y diagonales: un paso más apagado
    slab: [62, 78, 104],      // losa de cimentación
    mast: [142, 162, 192],    // mástil de la cima
    platform: [96, 116, 146], // plataforma de servicio
    panel: [167, 139, 250],   // antena panel: acento violeta
    dish: [34, 211, 238],     // disco: acento cian
    omni: [245, 177, 75],     // antena omni: acento ámbar
    sensor: [34, 211, 238],   // marcador del punto de montaje: acento cian
  };

  // ─────────────────── configuración de la geometría ─────────────────────
  // La geometría del modelo es configurable para que cada prueba de campo
  // pueda reproducir la estructura real (altura, tipo de antena, etc.) sin
  // tocar código. Los valores por defecto describen la torre de referencia
  // de la tesis y son los que se restauran con "Restaurar predeterminados".

  const DEFAULT_CFG = {
    height: 20,        // altura de la celosía (m)
    baseWidth: 6,      // lado del cuadrado en la base (m)
    topWidth: 2.4,     // lado del cuadrado en la cima (m)
    segments: 10,      // tramos de la celosía
    mastHeight: 3.6,   // altura del mástil sobre la cima (m)
    antenna: "panel+disco", // panel | disco | panel+disco | omni | ninguna
    panelW: 0.18,      // espesor del panel (m)
    panelH: 1.5,       // alto del panel (m)
    panelD: 0.85,      // ancho del panel (m)
    dishD: 1.9,        // diámetro del disco de microondas (m)
    omniL: 1.6,        // longitud del elemento omni (m)
    omniR: 0.22,       // radio de los discos del elemento omni (m)
    sensorPos: 0.5,    // posición del sensor (fracción de la altura, 0–1)
    // Ganancia visual del balanceo (m de deflexión en la cima por m/s² de
    // vib_rms). vib_rms es |‖a‖−g|: una sacudida lateral de amplitud A solo
    // mueve esa magnitud en A²/(2g), de modo que un apretón de mano de ~3 m/s²
    // laterales da vib_rms ≈ 0.3 m/s². Con ganancia 3 eso son ~0.9 m de
    // balanceo en una torre de 20 m (≈ 3°, claramente visible) sin salirse
    // del tope del 12 % de H.
    swayGain: 3.0,
  };

  const ANTENNA_TYPES = ["panel", "disco", "panel+disco", "omni", "ninguna"];

  // Rangos de validación: [mínimo, máximo]. Todo valor fuera de rango o no
  // finito se recorta al rango; el modelo nunca debe romperse por una entrada
  // manual inválida en el formulario.
  const CFG_RANGES = {
    height: [6, 60],
    baseWidth: [2, 16],
    topWidth: [0.8, 12],
    segments: [3, 16],
    mastHeight: [0, 15],
    panelW: [0.08, 1],
    panelH: [0.3, 6],
    panelD: [0.2, 4],
    dishD: [0.4, 6],
    omniL: [0.5, 6],
    omniR: [0.08, 1],
    sensorPos: [0.05, 0.98],
    swayGain: [0.05, 20],
  };

  /** Normaliza una configuración parcial: recorta rangos y completa huecos. */
  function sanitizeCfg(raw) {
    const out = {};
    for (const key of Object.keys(DEFAULT_CFG)) {
      const def = DEFAULT_CFG[key];
      let v = raw && raw[key] !== undefined ? raw[key] : def;
      if (key === "antenna") {
        out.antenna = ANTENNA_TYPES.indexOf(v) >= 0 ? v : def;
        continue;
      }
      v = Number(v);
      if (!Number.isFinite(v)) v = def;
      const range = CFG_RANGES[key];
      if (range) v = clamp(v, range[0], range[1]);
      if (key === "segments") v = Math.round(v);
      out[key] = v;
    }
    // La cima no puede ser más ancha que la base: una celosía real se estrecha.
    if (out.topWidth > out.baseWidth) out.topWidth = out.baseWidth;
    return out;
  }

  // ─────────────────────────── utilidades ────────────────────────────────

  /** Envuelve un ángulo al rango ]-180, 180]. */
  function wrap180(a) {
    a = a % 360;
    if (a > 180) a -= 360;
    if (a <= -180) a += 360;
    return a;
  }

  function clamp(v, lo, hi) {
    return v < lo ? lo : v > hi ? hi : v;
  }

  /**
   * Escribe en out (9 números, fila mayor) la matriz de rotación
   * R = Rz(yawEff) · Ry(pitch) · Rx(rollEff), exactamente el orden de
   * composición del contrato (CONTRATO §5) y de drawScene() en Processing.
   * La inversión de roll y yaw se aplica aquí para no repetirla en cada uso.
   */
  function rotationMatrix(out, rollDeg, pitchDeg, yawDeg) {
    const r = (INVERT_ROLL ? -rollDeg : rollDeg) * DEG_TO_RAD;
    const p = pitchDeg * DEG_TO_RAD;
    const y = (INVERT_YAW ? -yawDeg : yawDeg) * DEG_TO_RAD;
    const sr = Math.sin(r), cr = Math.cos(r);
    const sp = Math.sin(p), cp = Math.cos(p);
    const sy = Math.sin(y), cy = Math.cos(y);
    // Producto cerrado Rz·Ry·Rx: se evita multiplicar 3 matrices por frame.
    out[0] = cy * cp;
    out[1] = cy * sp * sr - sy * cr;
    out[2] = cy * sp * cr + sy * sr;
    out[3] = sy * cp;
    out[4] = sy * sp * sr + cy * cr;
    out[5] = sy * sp * cr - cy * sr;
    out[6] = -sp;
    out[7] = cp * sr;
    out[8] = cp * cr;
    return out;
  }

  // ───────────────────────── construcción de geometría ───────────────────
  // Toda la geometría es estática entre cambios de configuración: se genera
  // una única vez por cfg y el bucle de animación solo lee estos arrays. Cada
  // viga es un prisma rectangular de 8 vértices y 6 caras; el remate incluye
  // una losa, una plataforma, un mástil, la(s) antena(s) pedida(s) y el
  // marcador del sensor.

  /** Añade una viga (prisma rectangular) entre dos puntos al modelo. */
  function addBeam(b, x0, y0, z0, x1, y1, z1, halfW, rgb) {
    const dx = x1 - x0, dy = y1 - y0, dz = z1 - z0;
    const len = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;
    const ux = dx / len, uy = dy / len, uz = dz / len;
    // Sección transversal: dos ejes perpendiculares al eje de la viga. La
    // referencia "arriba" se elige para no ser paralela a la viga (si la
    // viga es casi vertical se usa +X como referencia alternativa).
    let rx = 0, ry = 1, rz = 0;
    if (Math.abs(uy) > 0.94) { rx = 1; ry = 0; rz = 0; }
    // side = u × ref
    let sx = uy * rz - uz * ry, sy = uz * rx - ux * rz, sz = ux * ry - uy * rx;
    const sl = Math.sqrt(sx * sx + sy * sy + sz * sz) || 1;
    sx /= sl; sy /= sl; sz /= sl;
    // up2 = side × u
    const vx = sy * uz - sz * uy, vy = sz * ux - sx * uz, vz = sx * uy - sy * ux;

    const base = b.v.length / 3;
    for (let end = 0; end < 2; end++) {
      const px = end ? x1 : x0, py = end ? y1 : y0, pz = end ? z1 : z0;
      for (let c = 0; c < 4; c++) {
        // Cuatro esquinas alrededor del eje: signos (+,+), (+,-), (-,-), (-,+)
        const ss = (c === 0 || c === 1) ? halfW : -halfW;
        const uu = (c === 0 || c === 3) ? halfW : -halfW;
        b.v.push(px + sx * ss + vx * uu, py + sy * ss + vy * uu, pz + sz * ss + vz * uu);
      }
    }
    // 6 caras: 2 tapas y 4 laterales (el orden de trazado es irrelevante:
    // la normal se voltea hacia la cámara y el pintado va por profundidad).
    const q = (a, c, d, e) => b.f.push({ iv: [base + a, base + c, base + d, base + e], rgb });
    q(0, 1, 2, 3); q(4, 5, 6, 7);
    q(0, 1, 5, 4); q(1, 2, 6, 5); q(2, 3, 7, 6); q(3, 0, 4, 7);
  }

  /** Añade una caja alineada con los ejes (losa, plataforma, panel, sensor). */
  function addBox(b, cx, cy, cz, sx, sy, sz, rgb) {
    const hx = sx / 2, hy = sy / 2, hz = sz / 2;
    const base = b.v.length / 3;
    for (let i = 0; i < 8; i++) {
      b.v.push(
        cx + (i & 1 ? hx : -hx),
        cy + (i & 2 ? hy : -hy),
        cz + (i & 4 ? hz : -hz)
      );
    }
    const q = (a, c, d, e) => b.f.push({ iv: [base + a, base + c, base + d, base + e], rgb });
    q(0, 1, 3, 2); q(4, 5, 7, 6);
    q(0, 1, 5, 4); q(2, 3, 7, 6);
    q(0, 2, 6, 4); q(1, 3, 7, 5);
  }

  /** Añade un disco poligonal en el plano XY (normal hacia +Z). */
  function addDisc(b, cx, cy, cz, radius, sides, rgb) {
    const base = b.v.length / 3;
    b.v.push(cx, cy, cz); // centro
    for (let i = 0; i < sides; i++) {
      const a = (i / sides) * Math.PI * 2;
      b.v.push(cx + Math.cos(a) * radius, cy + Math.sin(a) * radius, cz);
    }
    for (let i = 0; i < sides; i++) {
      const n = base + 1 + i, m = base + 1 + ((i + 1) % sides);
      // twoSided: los abanicos (disco de microondas, elementos omni) son
      // superficies SIN espesor, visibles por ambas caras. No se les aplica
      // el descarte de caras traseras que sí es seguro en los sólidos
      // convexos cerrados de addBeam/addBox.
      b.f.push({ iv: [base, n, m], rgb, twoSided: true });
    }
  }

  /**
   * Torre de celosía de 4 patas parametrizada por cfg (ver DEFAULT_CFG):
   * base cuadrada que se estrecha tramo a tramo, anillo horizontal y cruz de
   * San Andrés (aspas) en las cuatro caras de cada tramo — lo que distingue
   * una torre real de una pirámide maciza. El remate es un mástil con la(s)
   * antena(s) configurada(s) y una plataforma de servicio; la base es una
   * losa de cimentación para que no flote.
   */
  function buildTower(cfg) {
    const b = { v: [], f: [] };
    const N_SEG = cfg.segments;
    const H = cfg.height;
    const W_BASE = cfg.baseWidth / 2;
    const W_TOP = cfg.topWidth / 2;
    // El espesor de las vigas escala suavemente con la altura para que una
    // torre de 60 m no parezca de alambre ni una de 6 m de hierro macizo.
    const beamScale = clamp(H / 20, 0.7, 2.2);
    const halfAt = (k) => W_BASE + (W_TOP - W_BASE) * (k / N_SEG);
    const yAt = (k) => (H * k) / N_SEG;

    // Esquinas del anillo k: cuadrado centrado en el eje Y.
    const corner = (k, i) => {
      const w = halfAt(k), y = yAt(k);
      const sx = i === 0 || i === 1 ? w : -w;
      const sz = i === 0 || i === 3 ? w : -w;
      return [sx, y, sz];
    };

    // 1. Anillos horizontales en cada nivel (incluidos base y cima).
    for (let k = 0; k <= N_SEG; k++) {
      for (let i = 0; i < 4; i++) {
        const a = corner(k, i), c = corner(k, (i + 1) % 4);
        addBeam(b, a[0], a[1], a[2], c[0], c[1], c[2], 0.085 * beamScale, RGB.brace);
      }
    }

    // 2. Patas: van estrechándose tramo a tramo (sección ancha → estrecha).
    for (let k = 0; k < N_SEG; k++) {
      for (let i = 0; i < 4; i++) {
        const a = corner(k, i), c = corner(k + 1, i);
        addBeam(b, a[0], a[1], a[2], c[0], c[1], c[2], 0.115 * beamScale, RGB.leg);
      }
    }

    // 3. Diagonales en aspa (cruz de San Andrés) en cada cara de cada tramo.
    for (let k = 0; k < N_SEG; k++) {
      for (let i = 0; i < 4; i++) {
        const j = (i + 1) % 4;
        const a0 = corner(k, i), a1 = corner(k, j);
        const b0 = corner(k + 1, i), b1 = corner(k + 1, j);
        addBeam(b, a0[0], a0[1], a0[2], b1[0], b1[1], b1[2], 0.06 * beamScale, RGB.brace);
        addBeam(b, a1[0], a1[1], a1[2], b0[0], b0[1], b0[2], 0.06 * beamScale, RGB.brace);
      }
    }

    // 4. Losa de cimentación: evita que la torre parezca flotar sobre el
    //    fondo del panel. Su cara superior queda a y = 0. Su lado escala con
    //    la base de la celosía (1.4× el lado) para que la proporción aguante
    //    en cualquier configuración.
    const slabSide = cfg.baseWidth * 1.4;
    addBox(b, 0, -0.3, 0, slabSide, 0.6, slabSide, RGB.slab);

    // 5. Remate: plataforma de servicio + mástil + antena(s) configurada(s).
    const platSide = cfg.topWidth * 1.2 + 0.4;
    addBox(b, 0, H + 0.18, 0, platSide, 0.16, platSide, RGB.platform);

    const mastH = cfg.mastHeight;
    const hasMast = mastH >= 0.5;
    if (hasMast) {
      addBeam(b, 0, H, 0, 0, H + mastH, 0, 0.09 * beamScale, RGB.mast);
    }

    // Antena panel: rectángulo vertical junto al mástil (o a la cima si no
    // hay mástil). El espesor (panelW) es el eje X; el ancho (panelD) va en Z.
    const wantPanel = cfg.antenna === "panel" || cfg.antenna === "panel+disco";
    const wantDish = cfg.antenna === "disco" || cfg.antenna === "panel+disco";
    const wantOmni = cfg.antenna === "omni";
    const antennaY = hasMast ? H + mastH * 0.58 : H + cfg.panelH / 2 + 0.3;
    if (wantPanel) {
      addBox(b, 0.62 * beamScale, antennaY, 0, cfg.panelW, cfg.panelH, cfg.panelD, RGB.panel);
    }

    // Disco de microondas: abanico poligonal con su brazo de sujeción. El
    // brazo crece con el radio del disco para que no se clave en la cara.
    if (wantDish) {
      const dishR = cfg.dishD / 2;
      const armY = hasMast ? H + mastH * 0.39 : H + 1.4;
      const armLen = dishR * 0.65 + 0.25;
      addBeam(b, 0, armY, 0, 0, armY, armLen, 0.05 * beamScale, RGB.mast);
      addDisc(b, 0, armY, armLen + dishR * 0.85, dishR, 12, RGB.dish);
    }

    // Antena omnidireccional: elemento vertical esbelto con tres discos
    // colineales (estilo dipolo colinear). Se remata sobre el mástil.
    if (wantOmni) {
      const omniTop = hasMast ? H + mastH : H + cfg.omniL;
      const omniBase = omniTop - cfg.omniL;
      addBox(b, 0, omniBase + cfg.omniL / 2, 0, 0.09, cfg.omniL, 0.09, RGB.omni);
      for (let i = 0; i < 3; i++) {
        const y = omniBase + cfg.omniL * (0.22 + 0.28 * i);
        addDisc(b, 0, y, 0, cfg.omniR, 10, RGB.omni);
      }
    }

    // 6. Marcador del punto de montaje del sensor. IDEALIZACIÓN: en la
    //    realidad el sensor va fijado a un nudo de la celosía; aquí se
    //    representa como un cubo de acento cian en la cara +X a la altura
    //    fraccionaria configurada (por defecto la media) para que el
    //    operador vea "dónde mide" el sistema. Su ancla (sensorAnchor) es el
    //    punto sobre el que pulsa el halo de vibración.
    const kSens = N_SEG * cfg.sensorPos;
    const sensX = halfAt(kSens) + 0.32 * beamScale;
    const sensY = H * cfg.sensorPos;
    const cube = 0.46 * beamScale;
    addBox(b, sensX, sensY, 0, cube, cube, cube, RGB.sensor);

    return {
      verts: new Float32Array(b.v),
      faces: b.f,
      sensorAnchor: [sensX + cube / 2, sensY, 0],
    };
  }

  // ─────────────────────────── renderizador ──────────────────────────────

  /**
   * TowerModel — modelo 3D de la torre: inclinación por orientación medida
   * (MPU6050) y balanceo por vibración medida (ADXL345).
   *
   * Uso desde app.js:
   *   const model = new TowerModel(canvas);
   *   model.setOrientation(roll, pitch, yaw);      // grados, del mensaje "sample"
   *   model.setVibration(rms, peak, ax, ay, az);   // m/s² y aceleración cruda
   *   model.setConfig(cfg);                        // geometría configurable
   *   model.setHasData(false);                     // estado sin datos
   * La cámara la controla el usuario (arrastrar, rueda, doble clic); la
   * orientación del modelo NUNCA se recalcula en el cliente: llega ya
   * calculada del servidor en derived.roll/pitch/yaw (CONTRATO §5). La
   * vibración tampoco se recalcula: llega en derived.vib_rms/vib_peak y la
   * dirección sale del residual de aceleración que llega en raw.
   */
  class TowerModel {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");

      // Configuración de la geometría (ver DEFAULT_CFG) y geometría estática
      // precalculada una sola vez por configuración.
      this.cfg = sanitizeCfg(null);
      const geo = buildTower(this.cfg);
      this._allocate(geo);

      // Buffers reciclados frame a frame (sin asignaciones en el bucle).
      this.rot = new Float64Array(9);      // matriz de orientación actual

      // Orientación: objetivo (lo que llega del sensor) y valor mostrado (el
      // que interpola hacia el objetivo). Solo se guarda el ÚLTIMO objetivo:
      // sin cola no hay atraso acumulado aunque los datos lleguen a 50 Hz.
      this.roll = 0; this.pitch = 0; this.yaw = 0;
      this.targetRoll = 0; this.targetPitch = 0; this.targetYaw = 0;

      // Vibración: amplitud del balanceo (m de deflexión en la cima),
      // dirección unitaria en el plano horizontal del mundo (X, Z) y halo
      // del sensor (0–1). Objetivo + valor suavizado, como la orientación.
      this.vibAmp = 0; this.vibAmpTarget = 0;
      this.vibHalo = 0; this.vibHaloTarget = 0;
      this.swayDir = [1, 0];
      this._dirRaw = [1e-3, 0];
      // Estimación de la gravedad por eje (EMA) para extraer el residual
      // dinámico que da la dirección del balanceo.
      this._grav = [0, 0, 9.80665];
      this._gravInit = false;
      this._swayPhase = 0;
      this._lastFrameMs = 0;

      this.hasData = false;
      this.paused = false;
      this.showAxes = true;
      this.showShadow = true;

      // Frame sucio: con datos en vivo los setters marcan _dirty a 50–100 Hz
      // y se dibuja cada frame; cuando el estado converge (sensor en reposo,
      // pausa, o flujo detenido) se salta el render y el canvas conserva el
      // último frame. Ahorro real en batería del puesto de vigilancia.
      this._dirty = true;
      this._swayDisp = 0;
      this._haloWorld = [0, 0, 0];

      // Cámara orbital: el usuario la mueve, los datos no. Encuadre pensado
      // para el panel ancho del dashboard: la torre llena la altura útil.
      this.camDefault = { az: 0.72, el: 0.30, dist: 40 };
      this.cam = { az: 0.72, el: 0.30, dist: 40 };
      this.target = [0, 9.2, 0]; // punto de mira: media altura de la torre
      this._frameCamera();

      // Caché de colores: el sombreado y la niebla cuantizan el RGB final,
      // y la cadena "rgb(r,g,b)" se reutiliza en vez de crear 1000 strings
      // nuevos por frame (presupuesto de 16 ms del contrato §6).
      this.colorCache = new Map();

      // Polígono de sombra reutilizado (elipse deformada por la inclinación).
      this.shadowPts = new Float32Array(26 * 2);

      // Base de cámara reutilizada entre _render, _drawShadow y _drawAxes:
      // una sola construcción de la base ortonormal por frame.
      this._basis = {
        cxw: 0, cyw: 0, czw: 0,
        fx: 0, fy: 0, fz: 0,
        rx: 0, ry: 0, rz: 0,
        ux: 0, uy: 0, uz: 0,
        f: 0,
      };

      this._initInteraction();
      this._loop = this._loop.bind(this);
      requestAnimationFrame(this._loop);
    }

    /** (Re)asigna los buffers al tamaño de la geometría dada. */
    _allocate(geo) {
      this.verts = geo.verts;               // vértices del modelo en metros
      this.faces = geo.faces;               // caras con índices y color base
      this.sensorAnchor = geo.sensorAnchor; // punto de montaje del sensor
      this.nVerts = geo.verts.length / 3;
      this.nFaces = geo.faces.length;
      this.world = new Float32Array(this.nVerts * 3); // tras la rotación medida
      this.view = new Float32Array(this.nVerts * 3);  // espacio de cámara
      this.screen = new Float32Array(this.nVerts * 2); // píxeles proyectados
      this.depth = new Float32Array(this.nFaces);      // profundidad de cada cara
      this.order = new Array(this.nFaces);             // índices a ordenar
      for (let i = 0; i < this.nFaces; i++) this.order[i] = i;
    }

    /** Deriva el encuadre de cámara por defecto de la altura configurada. */
    _frameCamera() {
      const total = this.cfg.height + this.cfg.mastHeight;
      this.target[0] = 0;
      this.target[1] = total * 0.39;
      this.target[2] = 0;
      this.camDefault.az = 0.72;
      this.camDefault.el = 0.30;
      this.camDefault.dist = clamp(total * 1.7, 18, 95);
    }

    // ────────────────────────── interfaz pública ─────────────────────────

    /** Nueva orientación medida (grados, campos derived del contrato). */
    setOrientation(roll, pitch, yaw) {
      if (this.paused) return;
      if (Number.isFinite(roll)) this.targetRoll = roll;
      if (Number.isFinite(pitch)) this.targetPitch = pitch;
      if (Number.isFinite(yaw)) this.targetYaw = yaw;
      this._dirty = true;
    }

    /**
     * Nueva vibración medida (ADXL345, o MPU6050 como respaldo).
     *
     * @param {number} rms   derived.vib_rms (m/s²) — gobierna la amplitud.
     * @param {number} peak  derived.vib_peak (m/s²) — gobierna el halo.
     * @param {number} ax    raw.accel_x (m/s²) — dirección del balanceo.
     * @param {number} ay    raw.accel_y
     * @param {number} az    raw.accel_z
     *
     * La dirección sale del residual horizontal (a − g_est) mapeado
     * ax→X del mundo, ay→Z, con el módulo montado plano (Z vertical). La
     * estimación de la gravedad es un EMA del propio vector de aceleración,
     * de modo que vale para cualquier orientación de montaje.
     */
    setVibration(rms, peak, ax, ay, az) {
      if (this.paused) return;
      this._dirty = true;
      if (Number.isFinite(rms)) {
        const maxSway = this.cfg.height * SWAY_MAX_FRAC;
        this.vibAmpTarget = clamp(rms * this.cfg.swayGain, 0, maxSway);
      }
      if (Number.isFinite(peak)) {
        // Saturación del halo a ~3 m/s²: por encima ya está "en rojo" y el
        // pulso no aporta más información.
        this.vibHaloTarget = clamp(peak / 3, 0, 1);
      }
      if (Number.isFinite(ax) && Number.isFinite(ay) && Number.isFinite(az)) {
        if (!this._gravInit) {
          this._grav = [ax, ay, az];
          this._gravInit = true;
        }
        this._grav[0] += (ax - this._grav[0]) * GRAVITY_EMA;
        this._grav[1] += (ay - this._grav[1]) * GRAVITY_EMA;
        this._grav[2] += (az - this._grav[2]) * GRAVITY_EMA;
        // Solo el residual horizontal da dirección de balanceo: el residual
        // vertical (martilleo) se representa con el halo, no con balanceo.
        const rx = ax - this._grav[0];
        const rz = ay - this._grav[1];
        this._dirRaw[0] += (rx - this._dirRaw[0]) * DIR_SMOOTH;
        this._dirRaw[1] += (rz - this._dirRaw[1]) * DIR_SMOOTH;
        const len = Math.sqrt(this._dirRaw[0] * this._dirRaw[0] + this._dirRaw[1] * this._dirRaw[1]);
        if (len > 1e-3) {
          this.swayDir[0] = this._dirRaw[0] / len;
          this.swayDir[1] = this._dirRaw[1] / len;
        }
      }
    }

    /** Sin datos: el modelo queda quieto y se avisa sobre el canvas. */
    setHasData(has) {
      const next = !!has;
      if (next !== this.hasData) this._dirty = true;
      this.hasData = next;
      if (!this.hasData) {
        // Sin flujo el balanceo se apaga: el objetivo cae a cero y la
        // amplitud suavizada lo sigue sin saltos.
        this.vibAmpTarget = 0;
        this.vibHaloTarget = 0;
      }
    }

    /** Pausar congela la orientación; la cámara sigue operativa. */
    setPaused(p) {
      const next = !!p;
      if (next !== this.paused) this._dirty = true;
      this.paused = next;
    }

    setShowAxes(show) {
      const next = !!show;
      if (next !== this.showAxes) this._dirty = true;
      this.showAxes = next;
    }
    setShowShadow(show) {
      const next = !!show;
      if (next !== this.showShadow) this._dirty = true;
      this.showShadow = next;
    }

    /**
     * Cambia la geometría de la torre/antena (cfg parcial, se completa con
     * los valores por defecto y se recorta a rangos seguros). Reconstruye la
     * geometría estática y reencuadra la cámara a la nueva altura.
     */
    setConfig(cfg) {
      this.cfg = sanitizeCfg(cfg);
      const geo = buildTower(this.cfg);
      this._allocate(geo);
      this.colorCache.clear();
      this._frameCamera();
      this.resetView();
      this._dirty = true;
      // La amplitud máxima depende de H: se re-recorta el objetivo actual.
      const maxSway = this.cfg.height * SWAY_MAX_FRAC;
      this.vibAmpTarget = clamp(this.vibAmpTarget, 0, maxSway);
      return this.cfg;
    }

    /** Configuración efectiva (ya saneada). */
    getConfig() {
      return this.cfg;
    }

    /** Restablecer la vista de cámara (no la orientación medida). */
    resetView() {
      this.cam.az = this.camDefault.az;
      this.cam.el = this.camDefault.el;
      this.cam.dist = this.camDefault.dist;
      this._dirty = true;
    }

    // ─────────────────────────── interacción ─────────────────────────────
    // Arrastrar = orbitar la cámara; rueda = zoom; doble clic = vista
    // inicial. La orientación del modelo viene de los datos, no del ratón.

    _initInteraction() {
      const c = this.canvas;
      let dragging = false, lastX = 0, lastY = 0;

      c.addEventListener("pointerdown", (ev) => {
        dragging = true;
        lastX = ev.clientX; lastY = ev.clientY;
        c.setPointerCapture(ev.pointerId);
      });
      c.addEventListener("pointermove", (ev) => {
        if (!dragging) return;
        const dx = ev.clientX - lastX, dy = ev.clientY - lastY;
        lastX = ev.clientX; lastY = ev.clientY;
        this.cam.az += dx * 0.008;
        // Elevación acotada: se evita gimbal visual dando la vuelta al suelo.
        this.cam.el = clamp(this.cam.el + dy * 0.006, -0.15, 1.25);
        this._dirty = true;
      });
      const stop = (ev) => {
        dragging = false;
        if (ev.pointerId !== undefined && c.hasPointerCapture(ev.pointerId)) {
          c.releasePointerCapture(ev.pointerId);
        }
      };
      c.addEventListener("pointerup", stop);
      c.addEventListener("pointercancel", stop);
      c.addEventListener("pointerleave", () => { dragging = false; });

      c.addEventListener("wheel", (ev) => {
        ev.preventDefault(); // sin esto la página entera hace scroll
        this.cam.dist = clamp(this.cam.dist * Math.exp(ev.deltaY * 0.0012), 16, 200);
        this._dirty = true;
      }, { passive: false });

      c.addEventListener("dblclick", () => this.resetView());
    }

    // ────────────────────────────── pintado ──────────────────────────────

    /**
     * ¿Ha convergido el suavizado con sus objetivos? Si además no hay frame
     * sucio, el canvas ya muestra el estado final y se puede saltar el render.
     */
    _converged() {
      return Math.abs(wrap180(this.targetRoll - this.roll)) < 0.02 &&
        Math.abs(wrap180(this.targetPitch - this.pitch)) < 0.02 &&
        Math.abs(wrap180(this.targetYaw - this.yaw)) < 0.02 &&
        Math.abs(this.vibAmpTarget - this.vibAmp) < 1e-4 &&
        Math.abs(this.vibHaloTarget - this.vibHalo) < 1e-3;
    }

    _loop(nowMs) {
      // Salto de frame: sin datos nuevos, sin cámara movida y con el
      // suavizado convergido, volver a rasterizar ~1000 caras daría el mismo
      // píxel. Un redimensionado siempre fuerza frame (el canvas se limpió).
      const dpr = global.devicePixelRatio || 1;
      const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
      const resized = this.canvas.width !== Math.round(w * dpr) ||
        this.canvas.height !== Math.round(h * dpr);
      if (!this._dirty && !resized && this._converged()) {
        requestAnimationFrame(this._loop);
        return;
      }
      this._dirty = false;
      try {
        this._render(nowMs);
      } catch (err) {
        // Un frame con datos corruptos no debe tumbar el dashboard entero.
        console.error("model3d:", err);
      }
      requestAnimationFrame(this._loop);
    }

    /**
     * Construye la base ortonormal de la cámara orbital y la reutiliza.
     * Convención del canvas: +X derecha, +Y arriba, +Z hacia el espectador.
     * Con la cámara en +Z mirando al origen: right = forward × up_mundo
     * da +X a la derecha y up_vista = right × forward da +Y arriba.
     */
    _camera(w, h) {
      const b = this._basis;
      const cam = this.cam, tgt = this.target;
      const ce = Math.cos(cam.el), se = Math.sin(cam.el);
      b.cxw = tgt[0] + cam.dist * ce * Math.sin(cam.az);
      b.cyw = tgt[1] + cam.dist * se;
      b.czw = tgt[2] + cam.dist * ce * Math.cos(cam.az);
      // forward = normalizar(objetivo - cámara)
      let fx = tgt[0] - b.cxw, fy = tgt[1] - b.cyw, fz = tgt[2] - b.czw;
      const fl = Math.sqrt(fx * fx + fy * fy + fz * fz) || 1;
      fx /= fl; fy /= fl; fz /= fl;
      b.fx = fx; b.fy = fy; b.fz = fz;
      // right = forward × up_mundo (up_mundo = +Y), con ry = 0 siempre.
      let rx = -fz, ry = 0, rz = fx;
      const rl = Math.sqrt(rx * rx + rz * rz) || 1;
      rx /= rl; rz /= rl;
      b.rx = rx; b.ry = ry; b.rz = rz;
      // up_vista = right × forward
      b.ux = ry * fz - rz * fy;
      b.uy = rz * fx - rx * fz;
      b.uz = rx * fy - ry * fx;
      b.f = h * 1.12; // distancia focal en píxeles (FOV implícito ~48°)
      return b;
    }

    _render(nowMs) {
      const canvas = this.canvas, ctx = this.ctx;
      const dpr = global.devicePixelRatio || 1;
      const w = canvas.clientWidth, h = canvas.clientHeight;
      if (w === 0 || h === 0) return;
      if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = "#121b2e";
      ctx.fillRect(0, 0, w, h);

      // 0. Avance del balanceo en tiempo real (no en muestras): la cadencia
      //    de dibujo es independiente de la del sensor, y el modelo debe
      //    seguir balanceándose suavemente entre mensajes. dt acotado: un
      //    frame tras una pestaña en segundo plano no debe dar un salto de
      //    fase gigante.
      const dt = this._lastFrameMs ? clamp((nowMs - this._lastFrameMs) / 1000, 0, 0.1) : 0.016;
      this._lastFrameMs = nowMs;
      if (!this.paused) {
        this._swayPhase = (this._swayPhase + SWAY_HZ * dt) % 1;
      }

      // 1. Suavizado exponencial hacia la última orientación recibida. La
      //    corrección usa el error angular desenvuelto (camino más corto):
      //    190° y -170° son el mismo estado, y sin/cos son periódicos, pero
      //    se envuelve el estado al rango canónico para que siga legible.
      this.roll = wrap180(this.roll + wrap180(this.targetRoll - this.roll) * SMOOTH);
      this.pitch = wrap180(this.pitch + wrap180(this.targetPitch - this.pitch) * SMOOTH);
      this.yaw = wrap180(this.yaw + wrap180(this.targetYaw - this.yaw) * SMOOTH);
      rotationMatrix(this.rot, this.roll, this.pitch, this.yaw);

      // Suavizado de la vibración: la amplitud y el halo siguen al objetivo
      // sin sacudidas; sin datos el objetivo ya está en cero.
      this.vibAmp += (this.vibAmpTarget - this.vibAmp) * SWAY_SMOOTH;
      this.vibHalo += (this.vibHaloTarget - this.vibHalo) * SWAY_SMOOTH;

      // 2. Cámara orbital: posición y base ortonormal de la vista.
      const cam = this.cam;
      const basis = this._camera(w, h);
      const cxw = basis.cxw, cyw = basis.cyw, czw = basis.czw;
      const rx = basis.rx, ry = basis.ry, rz = basis.rz;
      const ux = basis.ux, uy = basis.uy, uz = basis.uz;

      // Deflexión instantánea del balanceo: seno de la fase visual. La
      // forma (y/H)² aproxima el primer modo de un voladizo (parábola con la
      // base fija): la base no se mueve y la cima se lleva toda la amplitud
      // (con el mástil por encima amplificando un poco más, como debe ser).
      const swayAmp = this.vibAmp;
      const swayDisp = swayAmp * Math.sin(this._swayPhase * Math.PI * 2);
      const dirX = this.swayDir[0], dirZ = this.swayDir[1];
      const invH = 1 / this.cfg.height;
      // El halo del sensor debe transformarse EXACTAMENTE igual que los
      // vértices; se guarda la deflexión del frame para reutilizarla en
      // _drawVibrationHalo sin repetir el cálculo (y sin riesgo de divergir).
      this._swayDisp = swayDisp;

      // 3. Rotar cada vértice con la matriz medida, aplicar la deflexión de
      //    balanceo en el plano horizontal del mundo y proyectar.
      const rot = this.rot, world = this.world, view = this.view, screen = this.screen;
      const nV = this.nVerts;
      for (let i = 0; i < nV; i++) {
        const x = this.verts[i * 3], y = this.verts[i * 3 + 1], z = this.verts[i * 3 + 2];
        let wx = rot[0] * x + rot[1] * y + rot[2] * z;
        let wy = rot[3] * x + rot[4] * y + rot[5] * z;
        let wz = rot[6] * x + rot[7] * y + rot[8] * z;
        if (swayDisp !== 0) {
          const t = y * invH;
          const d = swayDisp * t * t;
          wx += dirX * d;
          wz += dirZ * d;
        }
        world[i * 3] = wx; world[i * 3 + 1] = wy; world[i * 3 + 2] = wz;
        const dx = wx - cxw, dy = wy - cyw, dz = wz - czw;
        const vx = dx * rx + dy * ry + dz * rz;
        const vy = dx * ux + dy * uy + dz * uz;
        const vz = dx * basis.fx + dy * basis.fy + dz * basis.fz;
        view[i * 3] = vx; view[i * 3 + 1] = vy; view[i * 3 + 2] = vz;
        // Proyección en perspectiva: +X derecha, +Y arriba en el canvas
        // (de ahí el signo menos en la Y de pantalla, cuyo eje apunta abajo).
        const inv = vz > 0.2 ? 1 / vz : 1 / 0.2;
        screen[i * 2] = w / 2 + basis.f * vx * inv;
        screen[i * 2 + 1] = h / 2 - basis.f * vy * inv;
      }

      // 4. Sombra proyectada en el suelo (si está activada).
      if (this.showShadow) this._drawShadow(ctx);

      // 5. Profundidad por cara (centroide en espacio de cámara) y ordenado.
      const faces = this.faces, depth = this.depth, order = this.order;
      const nF = this.nFaces;
      for (let i = 0; i < nF; i++) {
        const iv = faces[i].iv;
        let d = 0;
        for (let k = 0; k < iv.length; k++) d += view[iv[k] * 3 + 2];
        depth[i] = d / iv.length;
      }
      // Painter's algorithm: de lejos a cerca. sort sobre el array de
      // índices ya reservado: cero asignaciones nuevas por frame.
      order.sort((a, b) => depth[b] - depth[a]);

      // 6. Pintado de caras con sombreado por normal y niebla por distancia.
      const fogNear = cam.dist * 0.5, fogSpan = cam.dist * 1.25;
      for (let oi = 0; oi < nF; oi++) {
        const fi = order[oi];
        const face = faces[fi];
        const iv = face.iv;
        const i0 = iv[0] * 3, i1 = iv[1] * 3, i2 = iv[2] * 3;
        // Normal de la cara a partir de los vértices YA rotados (espacio
        // del mundo): así el foco fijo ilumina la torre aunque la cámara
        // orbite y aunque la torre se incline.
        const ax = world[i1] - world[i0], ay = world[i1 + 1] - world[i0 + 1], az = world[i1 + 2] - world[i0 + 2];
        const bx = world[i2] - world[i0], by = world[i2 + 1] - world[i0 + 1], bz = world[i2 + 2] - world[i0 + 2];
        let nx = ay * bz - az * by, ny = az * bx - ax * bz, nz = ax * by - ay * bx;
        const nl = Math.sqrt(nx * nx + ny * ny + nz * nz) || 1;
        nx /= nl; ny /= nl; nz /= nl;
        // Prueba de visibilidad (backface culling): el producto punto de la
        // normal con el vector cámara→vértice dice qué lado de la cara mira a
        // la cámara. Las vigas y las cajas son sólidos convexos CERRADOS: su
        // cara trasera queda siempre tapada por la frontal en el orden
        // painter, así que se descarta y se ahorra ~50 % de fill+stroke por
        // frame. Los abanicos sin espesor (addDisc) se pintan por ambas caras.
        const c0 = iv[0];
        const dirx = world[c0 * 3] - cxw, diry = world[c0 * 3 + 1] - cyw, dirz = world[c0 * 3 + 2] - czw;
        const back = (nx * dirx + ny * diry + nz * dirz) > 0;
        if (back && !face.twoSided) continue;
        // Se voltea la normal hacia la cámara: el lado visible siempre se
        // ilumina de forma consistente, sin depender del sentido del trazado.
        if (back) { nx = -nx; ny = -ny; nz = -nz; }

        let diff = nx * LIGHT[0] + ny * LIGHT[1] + nz * LIGHT[2];
        if (diff < 0) diff = 0;
        const shade = AMBIENT + (1 - AMBIENT) * diff;

        // Niebla suave: las caras lejanas se mezclan con el fondo del panel.
        const fog = clamp((depth[fi] - fogNear) / fogSpan, 0, 0.72);
        const base = face.rgb;
        const r = Math.round((base[0] * shade) * (1 - fog) + FOG_RGB[0] * fog);
        const g = Math.round((base[1] * shade) * (1 - fog) + FOG_RGB[1] * fog);
        const bl = Math.round((base[2] * shade) * (1 - fog) + FOG_RGB[2] * fog);

        // Color cacheado por RGB cuantizado: sin cadenas nuevas por frame.
        const key = ((r >> 3) << 10) | ((g >> 3) << 5) | (bl >> 3);
        let style = this.colorCache.get(key);
        if (style === undefined) {
          style = "rgb(" + ((r >> 3) << 3) + "," + ((g >> 3) << 3) + "," + ((bl >> 3) << 3) + ")";
          this.colorCache.set(key, style);
        }

        ctx.beginPath();
        ctx.moveTo(screen[iv[0] * 2], screen[iv[0] * 2 + 1]);
        for (let k = 1; k < iv.length; k++) ctx.lineTo(screen[iv[k] * 2], screen[iv[k] * 2 + 1]);
        ctx.closePath();
        ctx.fillStyle = style;
        ctx.fill();
        // Trazo del mismo color: elimina las costuras entre caras contiguas
        // que deja el antialiasizado del relleno.
        ctx.strokeStyle = style;
        ctx.lineWidth = 1;
        ctx.stroke();
      }

      // 7. Ejes de referencia dibujados encima de la geometría (son una
      //    ayuda de lectura, no parte de la escena sombreada).
      if (this.showAxes) this._drawAxes(ctx, w, h);

      // 8. Halo de vibración en el punto de montaje del sensor.
      this._drawVibrationHalo(ctx, w, h);

      // 9. Estado sin datos: el modelo queda quieto y se avisa sin romper la UI.
      if (!this.hasData) {
        ctx.fillStyle = "rgba(232, 238, 252, 0.92)";
        ctx.font = "600 15px system-ui, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("Sin datos — esperando sensor", w / 2, 22);
      }
    }

    /**
     * Halo pulsátil sobre el punto de montaje del sensor: dos anillos cuyo
     * radio y opacidad siguen a vib_peak (0–1). Comunica "aquí se mide y aquí
     * está entrando la energía de vibración" sin competir con la geometría.
     *
     * El ancla se transforma AQUÍ igual que los vértices en _render (rotación
     * medida + deflexión de balanceo del frame) antes de proyectarla: si se
     * proyectara en coordenadas de modelo, el halo se despegaría del cubo del
     * sensor en cuanto la torre se inclinase o balancease — y como el yaw
     * deriva en datos reales, llegaría a orbitar la torre.
     */
    _drawVibrationHalo(ctx, w, h) {
      const halo = this.vibHalo;
      const a = this.sensorAnchor;
      const rot = this.rot;
      // Misma transformación que el bucle de vértices: R·p y, encima, la
      // deflexión horizontal (y/H)²·swayDisp del frame actual.
      let ax = rot[0] * a[0] + rot[1] * a[1] + rot[2] * a[2];
      let ay = rot[3] * a[0] + rot[4] * a[1] + rot[5] * a[2];
      let az = rot[6] * a[0] + rot[7] * a[1] + rot[8] * a[2];
      const swayDisp = this._swayDisp;
      if (swayDisp) {
        const t = a[1] / this.cfg.height;
        const d = swayDisp * t * t;
        ax += this.swayDir[0] * d;
        az += this.swayDir[1] * d;
      }
      // Posición mundial del ancla transformada: la expone el modelo para
      // que el smoke test pueda comprobar que el halo sigue al marcador.
      this._haloWorld = [ax, ay, az];
      if (halo < 0.02) return;
      const b = this._camera(w, h);
      const dx = ax - b.cxw, dy = ay - b.cyw, dz = az - b.czw;
      const vx = dx * b.rx + dy * b.ry + dz * b.rz;
      const vy = dx * b.ux + dy * b.uy + dz * b.uz;
      const vz = dx * b.fx + dy * b.fy + dz * b.fz;
      const inv = vz > 0.2 ? 1 / vz : 1 / 0.2;
      const sx = w / 2 + b.f * vx * inv;
      const sy = h / 2 - b.f * vy * inv;

      const pulse = 0.85 + 0.15 * Math.sin(this._swayPhase * Math.PI * 4);
      ctx.lineWidth = 2;
      ctx.strokeStyle = "rgba(34, 211, 238, " + (0.14 + 0.5 * halo * pulse).toFixed(3) + ")";
      ctx.beginPath();
      ctx.arc(sx, sy, 12 + 30 * halo * pulse, 0, Math.PI * 2);
      ctx.stroke();
      ctx.strokeStyle = "rgba(34, 211, 238, " + (0.08 + 0.32 * halo * pulse).toFixed(3) + ")";
      ctx.beginPath();
      ctx.arc(sx, sy, 22 + 52 * halo * pulse, 0, Math.PI * 2);
      ctx.stroke();
    }

    /** Elipse atenuada en el suelo que se deforma y desplaza con la inclinación. */
    _drawShadow(ctx) {
      // La inclinación desplaza la cima de la torre en el plano del suelo:
      // el vector up del modelo rotado, R·(0,1,0), es la SEGUNDA columna de
      // la matriz rot (fila mayor): de ahí rot[1] y rot[7].
      const rot = this.rot;
      const ux = rot[1], uz = rot[7];
      const tilt = Math.sqrt(ux * ux + uz * uz);
      // Centro del sombreado: a mitad de camino entre la base y la proyección
      // vertical de la cima (altura efectiva de la celosía + mástil).
      const H = this.cfg.height + this.cfg.mastHeight;
      const cx = ux * H * 0.5, cz = uz * H * 0.5;
      const dirX = tilt > 1e-4 ? ux / tilt : 1, dirZ = tilt > 1e-4 ? uz / tilt : 0;
      // Eje largo del elipse alineado con la dirección de la inclinación; el
      // alargamiento crece con el tilt para sugerir una sombra proyectada.
      const baseR = 1.8 * clamp(this.cfg.baseWidth / 6, 0.5, 2.5);
      const a = baseR + tilt * H * 0.85, bAxis = baseR * (1 - tilt * 0.35);
      const pts = this.shadowPts;
      const n = 25;
      for (let i = 0; i < n; i++) {
        const ang = (i / n) * Math.PI * 2;
        const ca = Math.cos(ang) * a, sa = Math.sin(ang) * bAxis;
        const px = cx + dirX * ca - dirZ * sa;
        const pz = cz + dirZ * ca + dirX * sa;
        // Proyección del punto del suelo con la misma cámara que la escena.
        pts[i * 2] = px; pts[i * 2 + 1] = pz; // se proyecta abajo
      }
      // Proyección con la MISMA cámara que la escena (y = 0.02 para no
      // solapar con la cara superior de la losa de cimentación).
      const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
      const b = this._camera(w, h);
      ctx.beginPath();
      for (let i = 0; i < n; i++) {
        const dx = pts[i * 2] - b.cxw, dy = 0.02 - b.cyw, dz = pts[i * 2 + 1] - b.czw;
        const vx = dx * b.rx + dy * b.ry + dz * b.rz;
        const vy = dx * b.ux + dy * b.uy + dz * b.uz;
        const vz = dx * b.fx + dy * b.fy + dz * b.fz;
        const inv = vz > 0.2 ? 1 / vz : 1 / 0.2;
        const sx = w / 2 + b.f * vx * inv, sy = h / 2 - b.f * vy * inv;
        if (i === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
      }
      ctx.closePath();
      // Atenuada a propósito: comunica contacto con el suelo sin competir
      // con la geometría sombreada.
      ctx.fillStyle = "rgba(2, 6, 16, 0.38)";
      ctx.fill();
    }

    /** Ejes de referencia desde la base con etiquetas (X roja, Y verde, Z azul). */
    _drawAxes(ctx, w, h) {
      const b = this._camera(w, h);
      // El par de puntos proyectados se reutiliza: seis llamadas por frame
      // no justifican crear arrays temporales dentro del bucle de dibujo.
      const p0 = [0, 0], p1 = [0, 0];
      const project = (x, y, z, out) => {
        const dx = x - b.cxw, dy = y - b.cyw, dz = z - b.czw;
        const vx = dx * b.rx + dy * b.ry + dz * b.rz;
        const vy = dx * b.ux + dy * b.uy + dz * b.uz;
        const vz = dx * b.fx + dy * b.fy + dz * b.fz;
        const inv = vz > 0.2 ? 1 / vz : 1 / 0.2;
        out[0] = w / 2 + b.f * vx * inv;
        out[1] = h / 2 - b.f * vy * inv;
      };
      // Longitud proporcional a la escena: los ejes crecen con la torre
      // para seguir siendo una referencia de escala legible.
      const L = clamp(this.cfg.height * 0.28, 3, 16);
      // Mismos colores que el visor de Processing (rojo X, verde Y, azul Z)
      // para que ambas representaciones sean reconocibles a la vez.
      const axes = [
        { to: [L, 0, 0], color: "#ff5a5a", label: "X" },
        { to: [0, L, 0], color: "#64eb96", label: "Y" },
        { to: [0, 0, L], color: "#64aaff", label: "Z" },
      ];
      ctx.lineWidth = 2;
      ctx.font = "600 12px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (const ax of axes) {
        project(0, 0, 0, p0);
        project(ax.to[0], ax.to[1], ax.to[2], p1);
        ctx.strokeStyle = ax.color;
        ctx.beginPath();
        ctx.moveTo(p0[0], p0[1]);
        ctx.lineTo(p1[0], p1[1]);
        ctx.stroke();
        ctx.fillStyle = ax.color;
        ctx.fillText(ax.label, p1[0], p1[1] - 10);
      }
    }
  }

  global.TesisModel3D = { TowerModel, DEFAULT_CFG, sanitizeCfg };
})(window);
