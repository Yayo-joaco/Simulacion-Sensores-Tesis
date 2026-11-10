# Visualización 3D de la estructura — orientación y vibración

Documento de diseño del modelo 3D del dashboard. Describe cómo se representan
las dos magnitudes estructurales que mide el sistema (inclinación y vibración),
qué ve el analista en pantalla y cómo se parametriza la geometría del modelo
para ajustarlo a cada estructura de prueba.

Tesis: *Diseño e implementación de un sistema IoT-WSN y CCTV para el monitoreo
de subsistemas críticos en un tramo de red troncal rural*.

Código: `web/js/model3d.js` (motor de render), `web/js/app.js` (cableado con el
protocolo, sección "modelo 3D"), `web/index.html` (HUD y formulario de
configuración). Protocolo de los datos: `CONTRATO_DE_DATOS.md` §4.2 y §5.

---

## 1. ¿Por qué un modelo 3D y no solo gráficas?

Las gráficas dicen *cuánto*; el modelo dice *qué le pasa a la estructura*.
Un analista que nunca ha visto la torre de Río Santiago entiende en dos
segundos, mirando el modelo, si la estructura se inclina, con qué intensidad
vibra y en qué dirección se mueve. El modelo es por tanto la pieza central
del panel: reproduce la celosía monitoreada y se mueve **solo** con datos
medidos — nunca con animación inventada.

El renderizador es propio (Canvas 2D, sin Three.js ni CDN) porque el sistema
debe funcionar sin Internet en el tramo rural (ver `ARQUITECTURA.md` §2.5).

## 2. Dos movimientos, dos sensores, dos partes de la estructura

El modelo combina dos fuentes independientes de movimiento, cada una
gobernada por su sensor y cada una sobre SU parte de la estructura. Pueden
actuar a la vez (antena orientada *y* torre vibrando) y ninguna de las dos
se recalcula en el navegador: el servidor las entrega ya calculadas en los
campos `derived` del mensaje `sample` (`CONTRATO_DE_DATOS.md` §5).

| Movimiento | Sensor | Campo del protocolo | Qué se ve |
|---|---|---|---|
| Orientación (cuerpo rígido) | MPU6050 | `derived.roll/pitch/yaw` (grados) | **Solo el conjunto antena** (mástil + antenas) se orienta, articulado en el nudo de la cima; la celosía permanece erguida y fija. Además, un modelo 3D aislado de la antena en la tarjeta de Orientación gira con los mismos tres ángulos |
| Vibración (respuesta dinámica) | ADXL345 | `derived.vib_rms`, `derived.vib_peak` (m/s²) | La celosía **balancea**; halo pulsátil en el punto de montaje del sensor |

La partición es deliberada: la orientación del MPU6050 describe cómo está
la ANTENA (la carga que monitorea la tesis), no una torre cayéndose; la
respuesta dinámica al viento es la que mueve la estructura entera.

Convención de ejes (idéntica en todo el proyecto, ver `processing/README.md`):
+X derecha, +Y arriba, +Z hacia el espectador; la rotación se compone como
R = Rz(yaw)·Ry(pitch)·Rx(roll), con la inversión de signo de roll/yaw que
replica el visor de Processing. Ambos visores se mueven igual ante la misma
muestra.

## 3. Cómo se representa la vibración

### 3.1 Balanceo con forma de primer modo aproximada

Cuando llega vibración, la celosía se deforma lateralmente con una **forma
aproximada de primer modo**: la parábola (y/H)², donde H es la altura de la
celosía — la base está fija y la deflexión crece con la altura hasta su
máximo en la cima. Esta parábola es la deflexión exacta de un voladizo bajo
momento flector constante y queda muy próxima al primer modo real del
voladizo (cuya serie de potencias empieza por el mismo término cuadrático);
se usa en lugar de la forma modal exacta porque es estable, barata de
evaluar por vértice y visualmente indistinguible a esta escala. Un ingeniero
estructural la lee sin leyenda: es la figura de un vástago vibrando en su
primer modo.

### 3.2 Amplitud — proporcional a lo medido, con exageración declarada

La deflexión en la cima vale:

```
deflexión (m) = ganancia_visual × vib_rms     [tope: 0.12 · H]
```

* `vib_rms` es la magnitud del contrato (§5): RMS de |‖a‖ − g| en ventana de
  1 s, en m/s². Es **dato medido**, sin adulterar.
* `ganancia_visual` (por defecto 3.0 m por m/s², configurable) amplifica el
  efecto para que sea visible. La necesidad de ganancia es doble: una vibración
  real de 0.5 m/s² desplazaría una torre de acero milímetros (invisibles en
  pantalla), y además la magnitud del contrato es de segundo orden para la
  vibración lateral (una sacudida de amplitud A mueve |‖a‖ − g| en A²/(2g)):
  un apretón de mano de ~3 m/s² laterales da vib_rms ≈ 0.3 m/s², que con la
  ganancia por defecto son ~0.9 m de balanceo en la cima de la torre de 20 m
  de referencia (≈ 3°, claramente visible). Esta exageración de amplitud es
  la convención estándar de las animaciones de respuesta estructural; el
  valor de la ganancia se muestra siempre en el HUD para que el analista sepa
  que la amplitud está amplificada.
* El tope del 12 % de H evita que una sacudida fuerte "deforme" la torre
  hasta el ridículo.

La amplitud se suaviza (interpolación exponencial) para que un pico aislado
no sacuda el modelo de golpe, y decae a cero cuando dejan de llegar muestras.

### 3.3 Frecuencia — ilustrativa, a propósito

El balanceo oscila a **1.6 Hz fijos**, una frecuencia visualmente tranquila y
del orden del primer modo real de una torre de ~20 m. **No es una frecuencia
medida**: el modelo comunica amplitud e intensidad, que sí lo son. La
frecuencia modal real es precisamente uno de los objetivos del futuro
capítulo de detección de anomalías (una torre dañada cambia su frecuencia
propia), y cuando esa métrica exista en el servidor sustituirá a esta
constante.

### 3.4 Dirección — sigue el movimiento real

El balanceo ocurre en la dirección del movimiento horizontal medido por el
sensor: se retira la gravedad del vector de aceleración crudo
(`raw.accel_*`) con un filtro exponencial y el residual horizontal da la
dirección (mapeo del módulo montado plano: ax→X del mundo, ay→Z). Si el
operador mueve el sensor de izquierda a derecha, la torre se balancea de
izquierda a derecha; si lo mueve hacia delante y atrás, se balancea hacia
delante y atrás. La dirección se suaviza para que el ruido no invierta el
balanceo.

### 3.5 Halo del punto de montaje

El cubo cian marca dónde está montado el sensor (altura configurable). Alrededor
se dibuja un halo pulsátil cuyo radio y opacidad siguen a `vib_peak`
(saturado a 3 m/s²): comunica "aquí entra la energía" y permite distinguir de
un vistazo un martilleo puntual (halo grande, poco balanceo) de una vibración
sostenida (balanceo grande).

### 3.6 Semáforo de vibración

La barra del HUD del modelo y la gráfica de vibración comparten **el mismo
umbral visual** (por defecto 0.5 m/s², editable, persistido en `localStorage`):

| Nivel | Condición (sobre vib_rms) | Color |
|---|---|---|
| NORMAL | < 0.6 · umbral | verde |
| PRECAUCIÓN | ≥ 0.6 · umbral y < umbral | ámbar |
| ALARMA | ≥ umbral | rojo |

Igual que el semáforo de inclinación, es **solo visual**: no dispara alertas.
La lógica de alertas pertenece al capítulo de detección de anomalías.

## 4. Qué ve el analista en el panel

1. **Torre de celosía** fija, balanceándose con la vibración (ADXL345) en
   tiempo real, con sombra proyectada y ejes de referencia; sobre ella, el
   conjunto antena orientándose con el MPU6050.
2. **HUD superior**: roll, pitch, yaw, inclinación total con semáforo,
   vibración RMS y vibración pico con barra de nivel frente al umbral.
3. **Halos de vibración** en el punto de montaje del sensor.
4. **Gráfica de vibración** del panel ADXL345 con las series `vib_rms` y
   `vib_peak` y la línea de umbral: el modelo da la intuición, la gráfica da
   el número exacto y el histórico.
5. **Configuración plegable** de la geometría (ver §5).
6. **Estado "Sin datos — esperando sensor"** sobre el canvas si no llegan
   muestras: el modelo nunca "actúa" sin datos.

Control de cámara (independiente de los datos): arrastrar orbita, rueda hace
zoom, doble clic restablece la vista. "Pausar" congela el modelo para
inspeccionar una posición sin perder el flujo de datos.

## 5. Geometría configurable

Cada prueba puede reproducir la estructura real sin tocar código. El formulario
*Configuración de la torre y la antena* (plegable, bajo el canvas) edita los
parámetros; "Aplicar" reconstruye la geometría y "Restaurar predeterminados"
vuelve a la torre de referencia de la tesis. Los valores se guardan en
`localStorage` (`tesis.model3d.geom`), sobreviven a recargas y solo cambian la
geometría dibujada — **ningún dato medido se toca**.

| Parámetro | Defecto | Rango | Efecto |
|---|---|---|---|
| Altura de la celosía (m) | 20 | 6 – 60 | Altura de la celosía; escala el encuadre y el tope del balanceo |
| Lado de la base (m) | 6 | 2 – 16 | Cuadrado inferior; también la losa de cimentación |
| Lado de la cima (m) | 2.4 | 0.8 – 12 | Cuadrado superior (no puede superar la base) |
| Tramos | 10 | 3 – 16 | Divisiones de la celosía (anillos + cruces de San Andrés) |
| Altura del mástil (m) | 3.6 | 0 – 15 | Mástil sobre la cima (0 = sin mástil; las antenas se montan entonces sobre la cima de la celosía) |
| Tipo de antena | Panel + disco | Panel / Disco / Panel+disco / Omni / Ninguna | Remate de la cima |
| Espesor / alto / ancho del panel (m) | 0.18 / 1.5 / 0.85 | 0.08–1 / 0.3–6 / 0.2–4 | Dimensiones de la antena panel |
| Diámetro del disco (m) | 1.9 | 0.4 – 6 | Disco de microondas y su brazo |
| Longitud / radio del omni (m) | 1.6 / 0.22 | 0.5–6 / 0.08–1 | Elemento omnidireccional |
| Posición del sensor (% altura) | 50 % | 5 – 98 % | Altura del punto de montaje sobre la celosía |
| Ganancia visual del balanceo | 3.0 | 0.05 – 20 | Amplificación visual de vib_rms (m por m/s²) |

Todas las entradas se sanean: un valor no finito o fuera de rango se recorta
o vuelve al predeterminado, y el modelo no puede romperse por un formulario
mal cumplimentado (comprobado en el smoke test de §7).

### Uso típico por prueba

* **Banco de laboratorio (hoy)**: valores predeterminados; subir la ganancia
  (p. ej. 2–5) para que el balanceo se vea bien con las sacudidas de mano.
* **Antena real en la PUCP**: introducir la altura y el tipo/dimensiones de la
  antena del modelo físico; ajustar la posición del sensor a la altura real de
  montaje.
* **Estación base de Río Santiago**: la geometría de la torre real (altura,
  sección, tipo de antena) más el umbral de vibración del ensayo; la
  configuración queda guardada en el puesto de vigilancia.

## 6. Notas de implementación (para el capítulo de la tesis)

* **Render propio en Canvas 2D**: proyección en perspectiva, rotación de
  vértices, painter's algorithm con sombreado por normal y niebla por
  distancia, y **descarte de caras traseras** (*backface culling*): las
  vigas y las cajas son sólidos convexos cerrados, cuya cara trasera queda
  siempre tapada por la frontal en el orden de pintado, así que se descartan
  — se ahorra ≈ 50 % de las operaciones de trazado por frame (los abanicos
  sin espesor de las antenas se pintan por ambas caras). La geometría se
  precalcula una vez por configuración (~1100 polígonos); el bucle de dibujo
  solo rota, desplaza, proyecta y rellena, reciclando todos sus arrays: 60
  fps estables (criterio del contrato §6). Además, un **frame sucio** gobierna
  el render: sin muestras nuevas, sin cámara movida y con el suavizado
  convergido se salta el rasterizado completo (el canvas conserva el último
  frame), lo que evita quemar GPU y batería con el puesto en reposo.
* **La orientación nunca se recalcula en el cliente**: llega en
  `derived.roll/pitch/yaw` del servidor (filtro complementario α = 0.96 en
  `server/derived.py`), lo que garantiza que dashboard, histórico y la futura
  detección de anomalías compartan exactamente los mismos valores.
* **El balanceo se dibuja en el espacio del mundo** (plano horizontal), no
  ligado a los ejes del modelo inclinado: la deflexión es estructural,
  medida respecto al suelo.
* **Partición estático/antena**: la geometría se construye en dos grupos de
  vértices contiguos — celosía (fija en el mundo) y conjunto antena
  (mástil + antenas) — y solo el segundo recibe la matriz de rotación de la
  orientación, articulada en el nudo de la cima. El modelo de antena de la
  tarjeta de Orientación (`AntennaModel`) comparte la MISMA geometría
  (`buildAntennaAssembly`), la MISMA configuración y el MISMO motor de
  pintado (`paintFaces`) que el modelo grande: dos visores, una sola fuente
  de verdad, y el analista ve la antena grande moverse exactamente igual que
  la pequeña.
* **Robustez**: un frame con datos corruptos no tumba el dashboard (el bucle
  atrapa excepciones y continúa); sin muestras en 3 s el modelo se para y
  avisa; un hueco de datos no se integra como una rotación gigante.

## 7. Verificación

* Smoke automatizado del motor (geometría, saneamiento de entradas, vibración,
  robustez del render):

  ```bash
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --headless=new --disable-gpu --dump-dom \
    "file://$(pwd)/tests/smoke_model3d.html"
  ```

  (el título de la página debe ser `SMOKE_PASS`; se ejecuta sin servidor).
* Prueba manual con sensor real: arrancar el servidor **sin** `--simulate`
  (`./.venv/bin/python -m server.run`), abrir `http://127.0.0.1:8000`,
  mover el ADXL345 y comprobar que el modelo se balancea en la dirección del
  movimiento, que la amplitud crece con la intensidad y que la barra de nivel
  responde al umbral. Después, mover el MPU6050 y comprobar que **solo la
  antena** se orienta (la celosía se queda erguida y fija), y que el modelo
  3D de la tarjeta de Orientación gira con los mismos roll/pitch/yaw que
  muestra el HUD.
