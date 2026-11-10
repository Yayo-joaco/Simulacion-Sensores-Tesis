# Arquitectura de la plataforma de monitoreo estructural

Documento técnico del subsistema IoT-WSN: capas, justificación de cada elección
tecnológica, flujo de datos de extremo a extremo, modelo de datos y estrategia
de escalado. La especificación normativa del protocolo vive en
[`CONTRATO_DE_DATOS.md`](CONTRATO_DE_DATOS.md); aquí se explica el **por qué**
de cada decisión.

---

## 1. Vista por capas

La plataforma se organiza en cinco capas con responsabilidades separadas. Cada
capa solo conoce la interfaz de la capa inferior, de modo que se puede sustituir
una pieza (por ejemplo, la base de datos) sin reescribir el resto.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 5. PRESENTACIÓN                                                      │
│    Dashboard web (HTML/CSS/JS puro, sin build, sin CDN)              │
│    · gráficas en tiempo real · orientación · vibración · histórico   │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │ HTTP + WebSocket (JSON)
┌────────────────────────────────▼─────────────────────────────────────┐
│ 4. API                                                               │
│    FastAPI: REST /api/*  ·  WebSocket /ws  ·  ingesta /api/ingest    │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │ objetos de dominio (muestras, eventos)
┌────────────────────────────────▼─────────────────────────────────────┐
│ 3. PROCESAMIENTO                                                     │
│    derived.py: magnitudes, roll/pitch/yaw, vib_rms / vib_peak        │
│    (una sola capa: visualización y detección de anomalías comparten  │
│     exactamente los mismos valores)                                  │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │ muestras normalizadas
┌────────────────────────────────▼─────────────────────────────────────┐
│ 2. INGESTA Y PARSEO                                                  │
│    ingest.py (USB serie 115200)  ·  parser de líneas del contrato    │
│    tolerante a basura · contador de parse_errors · watchdog 2 s      │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │ líneas UTF-8 terminadas en \n
┌────────────────────────────────▼─────────────────────────────────────┐
│ 1. NODO SENSOR                                                       │
│    ESP32 + MPU6050, ADXL345, HX711, DS18B20, anemómetro, veleta     │
│    muestreo 50 Hz · validación de rango físico · diagnósticos [..]   │
└──────────────────────────────────────────────────────────────────────┘

                    persistencia transversal:
        db.py → Repository → SQLite (data/monitoreo.db)
```

### 1.1 Responsabilidades por capa

| Capa | Hace | No hace |
|---|---|---|
| Nodo sensor | muestrear a 50 Hz, validar rangos físicos, emitir el protocolo | calcular métricas derivadas, decidir umbrales |
| Ingesta | abrir el puerto, partir líneas, descartar basura, marcar `connected` | interpretar la señal |
| Procesamiento | magnitudes, orientación, vibración en ventana de 1 s | persistir ni formatear para pantalla |
| Persistencia | transacciones, índices, auditoría del payload crudo, retención y agregación (`retention.py` + `db.py`) | decidir umbrales ni alertar |
| API | serializar JSON, repartir a clientes, aceptar ingesta por red | lógica de negocio |
| Presentación | dibujar, interpolar, alertar visualmente | recalcular métricas (solo convierte unidades al mostrar) |

La separación que más peso tiene en la tesis es la de **procesamiento**:
al calcular las métricas derivadas en un único punto del backend, el dashboard,
una futura rutina de detección de anomalías y la exportación CSV consumen
exactamente los mismos números. Si cada consumidor recalculase su propia
versión, las diferencias de implementación (grados vs radianes, ventana
distinta, gravedad distinta) contaminarían cualquier umbral calibrado.

## 2. Justificación de la pila tecnológica

### 2.1 Python + FastAPI

Se eligió Python con FastAPI frente a alternativas como Node.js, Go o C++:

* **Velocidad de desarrollo**: la carga de trabajo real del sistema es de
  ~100 muestras/s (2 sensores a 50 Hz), muy por debajo de lo que este stack
  procesa sin esfuerzo. En un proyecto de tesis el recurso escaso es el tiempo
  de ingeniería, no el CPU.
* **WebSocket integrado**: FastAPI expone WebSocket con la misma aplicación y
  el mismo esquema de validación que la parte REST, sin servidores adicionales
  ni bibliotecas de protocolo.
* **Continuidad con el análisis**: la detección de anomalías y la predicción de
  la etapa siguiente (numpy, scikit-learn, statsmodels) se añaden como módulos
  más del mismo proceso, sin puente entre lenguajes ni doble serialización.
* **Ecosistema de hardware**: PySerial resuelve la ingesta USB con una
  interfaz de líneas, natural para el protocolo de texto del ESP32.
* **Async de verdad**: el bucle de lectura serie corre en un hilo propio y la
  API es `async`, de modo que una ingesta de 50 Hz no bloquea las respuestas
  HTTP (criterio de calidad del contrato).

El coste de esta decisión es la menor velocidad cruda frente a Go o C++, que en
esta escala es irrelevante, y el GIL, que aquí no molesta porque el trabajo es
de E/S (serie, disco, red) y no de cómputo intensivo.

### 2.2 SQLite — y por qué no InfluxDB de entrada

La persistencia es un único archivo `data/monitoreo.db` con SQLite:

* **Cero configuración**: no hay demonio que instalar, autenticar ni
  mantener. En campo, cada pieza móvil es una pieza que puede fallar.
* **Portable y exportable**: la base de datos es un archivo que se copia, se
  adjunta a la memoria de la tesis como evidencia y se abre en cualquier
  máquina. Eso es valioso para la trazabilidad de un trabajo académico.
* **Transaccional**: las escrituras son atómicas y consistentes, y el lector
  del dashboard no ve estados intermedios.
* **Suficiente**: el volumen de un nodo es ~8,6 millones de filas/día
  (100 muestras/s × 86 400 s). Con el índice `(sensor, ts_ms)` las consultas
  por ventana son rápidas durante la escala de tiempo de una campaña de
  medición de tesis.

**Por qué no se usó InfluxDB desde el inicio**, siendo la opción natural para
series temporales:

* **Complejidad operativa**: InfluxDB implica un servicio aparte (frecuentemente
  Docker), configuración de buckets, retención y autenticación. Cada uno de esos
  puntos es una causa potencial de fallo en una demostración de campo.
* **Curva de aprendizaje**: su lenguaje de consulta (Flux/InfluxQL), su modelo
  de esquema por *measurement/tags/fields* y sus herramientas de visualización
  son un segundo sistema que el tesista debe dominar para un volumen que no lo
  exige.
* **Pérdida de simplicidad reproducible**: replicar el entorno de la tesis
  pasaba de «clonar y ejecutar» a «clonar, instalar Docker, levantar el
  contenedor, importar los datos».
* **Acoplamiento temprano**: elegir el modelo de *tags* de InfluxDB demasiado
  pronto habría fijado el esquema de los sensores antes de tener claro qué
  magnitudes son estables.

**Cuándo sí tendría sentido migrar:**

| Señal | Motivo |
|---|---|
| Varios nodos y retención de meses | SQLite crece en un solo archivo y las consultas agregadas se degradan |
| Consultas agregadas continuas (p. ej. RMS por hora sobre todo el histórico) | InfluxDB/TimescaleDB tienen agregados y *downsampling* nativos |
| Alta disponibilidad con escritores concurrentes | SQLite admite un solo escritor |
| Necesidad de alertas y ventanas temporales del lado de la base | funciones de ventana y *continuous queries* propias de las series temporales |

La migración está prevista en el diseño: todo acceso a datos pasa por la capa
de repositorio (`db.py` → clase `Repository`), que concentra las consultas. La
API y el procesamiento no conocen SQLite; conocen el repositorio. Cambiar a
InfluxDB o a PostgreSQL+TimescaleDB significa implementar el mismo interfaz
sobre otro almacén, sin tocar el resto del sistema. Este patrón —*repository
como cortafuegos de la tecnología de almacenamiento*— es el que hace que la
elección inicial sea reversible.

### 2.3 WebSocket frente a polling HTTP y SSE

El dashboard necesita telemetría en vivo. Las tres opciones evaluadas:

| Criterio | Polling HTTP | SSE | **WebSocket (elegido)** |
|---|---|---|---|
| Latencia | del orden del intervalo de sondeo (0,5–2 s) | baja (push) | baja (push) |
| Carga por mensaje | una petición HTTP completa cada vez | cabeceras HTTP por evento | trama mínima, JSON solo con datos |
| Direccionalidad | cliente → servidor | servidor → cliente | **bidireccional** |
| Alertas futuras (cliente → nodo) | requiere canal aparte | requiere canal aparte | natural sobre el mismo canal |
| 100 muestras/s a varios clientes | multiplica peticiones | razonable | el servidor hace *broadcast* de cada muestra |

* **Polling** es simple pero mala elección a 50 Hz por sensor: para refrescar
  cada 500 ms con dos sensores son 4 peticiones/s por cliente, casi todas
  redundantes, con la latencia media de medio periodo de sondeo. Para una
  señal de vibración cuya utilidad está en los transitorios, eso es perder
  justo lo que interesa.
* **SSE** habría resuelto el envío servidor→cliente con menos complejidad, y
  es una alternativa válida si algún día se necesita compatibilidad con
  clientes antiguos. Se descartó porque el caso de uso previsto incluye
  alertas y comandos del lado del usuario (silenciar una alarma, pedir una
  recalibración), es decir, tráfico cliente→servidor que SSE obliga a abrir
  en un canal HTTP aparte.
* **WebSocket** mantiene un canal único y bidireccional, con un mensaje JSON
  por muestra. La topología del servidor es una tarea de *broadcast*: cada
  mensaje válido se reparte a todos los clientes conectados, de modo que añadir
  espectadores no añade carga de ingesta.

El formato de los mensajes (`sample`, `status`, `log`) está fijado en el
contrato, de manera que un consumidor distinto del dashboard (un script de
captura, un panel secundario, la etapa de detección de anomalías) puede
suscribirse sin convenios privados.

### 2.4 Doble camino de ingesta: USB serie y HTTP

| Camino | Uso | Por qué existe |
|---|---|---|
| USB serie → `ingest.py` | laboratorio, con el ESP32 cableado | valida electrónica y firmware sin depender de la red |
| `POST /api/ingest` | despliegue sin cable (Wi-Fi / pasarela) | es el camino del despliegue rural |

El tramo de red troncal rural objeto de estudio tiene cobertura inalámbrica
escasa e intermitente. Si la única forma de recibir datos fuese la red, la
primera prueba del sistema dependería de que la radio funcionase, mezclando dos
fuentes de fallo (electrónica y enlace) en un solo síntoma. Con la ingesta USB,
la electrónica y el firmware se validan de forma independiente, y la red se
prueba después sobre un sistema ya conocido.

Ambos caminos convergen en el mismo parseo y la misma persistencia: el
`ts_ms` de la muestra y su `payload` crudo quedan registrados, pero el
dashboard no sabe ni le importa por qué camino llegaron. Esa indiferencia es
deliberada: el camino de ingesta es un detalle de despliegue, no una propiedad
de la telemetría.

### 2.5 Frontend sin build, sin framework y sin CDN

* **Sin CDN**: el sistema opera donde no hay Internet de confianza. Un gráfico
  que se cae porque no cargó una fuente tipográfica de un tercero es un gráfico
  inútil en campo. Todo el CSS/JS es local.
* **Sin build**: sin Node ni npm, el ciclo de modificación es «guardar y
  recargar». Además reduce la superficie de dependencias del proyecto (en un
  repositorio de tesis, cada `node_modules` es deuda de reproducibilidad).
* **Sin framework JS**: la interfaz son unas pocas vistas y series temporales
  que se actualizan con `requestAnimationFrame` sobre un `<canvas>`. Un
  framework aportaría reactividad que aquí se resuelve con un búfer circular de
  puntos por métrica.

### 2.6 Métricas de vibración como base de umbrales

La detección estructural de la etapa posterior necesita una señal de
referencia. Se eligió el **RMS de la aceleración de vibración en ventana de
1 segundo** (`vib_rms`), con su pico asociado (`vib_peak`), porque:

* el RMS integra la energía de la vibración en la ventana y es robusto al ruido
  puntual, frente al pico, que es útil pero ruidoso;
* una ventana de 1 s es corta frente a los modos estructurales de interés de la
  torre y larga frente a la tasa de muestreo (50 muestras a 50 Hz), un
  compromiso razonable entre resolución temporal y estabilidad;
* al calcularla en el servidor junto al resto de métricas, el umbral que se
  calibre en el capítulo de detección se aplica exactamente sobre la misma
  serie que se visualiza y que se exporta.

El mismo razonamiento se aplica a `accel_mag`, `gyro_mag` y a la orientación:
son la interfaz pública de la señal, y todo umbral futuro se define sobre ellas.

## 3. Flujo de datos de extremo a extremo

```
  ESP32                    servidor                          navegadores
    │                          │                                  │
    │  "DATA,0.121,-0.043,…\n" │                                  │
    │ ── serie 115200 ───────► │                                  │
    │                          │ 1. hilo serie lee la línea       │
    │                          │ 2. parser: prefijo DATA/ADXL/[   │
    │                          │    · no finito → descarta línea  │
    │                          │    · desconocido → ignora        │
    │                          │ 3. derived.py → roll/pitch/yaw,  │
    │                          │    vib_rms/vib_peak (ventana 1s) │
    │                          │ 4. Repository.insert_sample()    │
    │                          │    → SQLite (transacción)        │
    │                          │ 5. broadcast JSON {"type":       │
    │                          │    "sample", raw, derived}       │
    │                          │ ──────────── WebSocket ────────► │
    │                          │                                  │ 6. búfer
    │                          │                                  │    circular
    │                          │                                  │ 7. canvas
    │                          │                                  │    a 60 fps
    │                          │                                  │
    │                          │ ◄── GET /api/history ─────────── │ 8. histórico
    │                          │ ──── series [ts, valor] ───────► │
```

Paso a paso:

1. **Captura**: el hilo de ingesta abre el puerto a 115200 baudios y lee hasta
   `\n`. La lectura ocurre fuera del bucle de eventos de async, para que la
   E/S serie no compita con las respuestas HTTP.
2. **Parseo**: solo se aceptan líneas que empiecen por `DATA,`, `ADXL,` o `[`.
   Eso descarta los restos del bootloader (74880 baudios) y el banner de
   arranque sin reglas especiales. Un campo no numérico descarta la línea
   completa (mejor un hueco que un dato corrupto) e incrementa `parse_errors`.
3. **Derivación**: una sola capa calcula magnitudes, orientación y vibración.
   Las líneas `[...]` no producen muestras: se registran como eventos con nivel
   (`info`/`warn`/`error`) según su contenido.
4. **Persistencia**: la muestra se inserta con `ts_ms` de recepción (o el que
   traiga el emisor) y su `payload` crudo en JSON, para auditoría y
   reprocesado. A la vez se actualiza `sensor_status`.
5. **Publicación**: el mensaje `sample` sale hacia todos los clientes
   conectados; los cambios de presencia se publican como `status`.
6–7. **Visualización**: el navegador acumula puntos en un búfer por métrica y
   redibuja con `requestAnimationFrame`, independientemente de la tasa de
   llegada. Las diferencias de unidad se resuelven aquí (rad/s → grados/s solo
   al etiquetar ejes).
8. **Histórico**: `GET /api/history` consulta por ventana y devuelve series
   `[ts_ms, valor]` listas para graficar; `GET /api/export.csv` materializa lo
   mismo para análisis fuera del navegador.

**Recuperación ante fallos.** Un reinicio del ESP32 se traduce en la ausencia
de líneas: a los 2 s el servidor marca `connected = false` y lo publica; al
volver la telemetría se vuelve a `true`. Un cuadro mal formado se descarta sin
interrumpir la ingesta. Ninguno de estos fallos detiene el proceso del
servidor.

## 4. Modelo de datos

Esquema completo y normativo en la sección 3 del contrato. Resumen y criterios:

```sql
samples        (id, ts_ms, sensor, accel_x/y/z, gyro_x/y/z, temp_c, payload)
events         (id, ts_ms, sensor, level, code, message)
sensor_status  (sensor PK, present, last_ts_ms, n_samples)
samples_agg    (bucket_ts_ms, sensor, metric, n, avg, min, max)
índices: idx_samples_sensor_ts (sensor, ts_ms), idx_samples_ts (ts_ms)
```

Decisiones de diseño:

* **`ts_ms` en epoch UTC con precisión de milisegundos.** Los 50 Hz se
  resuelven con holgura (periodo de 20 ms) y el formato es directo para
  graficar, ordenar y exportar sin conversiones de zona horaria. La marca de
  tiempo se toma a la recepción salvo que el emisor envíe `ts_ms`, lo que se
  documenta como **sincronización por llegada**: válido para una ventana de
  visualización y para un nodo, insuficiente para correlacionar nodos
  distantes (ver limitaciones).
* **Unidad de `gyro_*` fijada en rad/s en base de datos.** La conversión a
  grados se hace al presentar. Tener dos unidades viviendo en la misma tabla
  es una fuente clásica de errores de factor 57,3; la regla «rad/s hasta el
  cristal» elimina la ambigüedad.
* **Columnas dedicadas para el IMU + `payload` JSON.** Las columnas permiten
  consultar y promediar por SQL; el `payload` conserva la línea cruda para
  auditoría, para reprocessar con otro algoritmo y para no perder información
  si el parser del futuro es más rico que el actual.
* **`events` separado de `samples`.** Un diagnóstico `[MPU6050] I2C ERROR` no
  es un número: meterlo en la tabla de muestras obligaría a nulos absurdos y
  ensuciaría cualquier agregado. Cada cosa en su tabla, y el log del dashboard
  se alimenta de `events`.
* **`sensor_status` denormalizado.** Es el estado caliente (¿hay datos? ¿desde
  cuándo? ¿cuántos?), consultado en cada conexión de cliente. Derivarlo con un
  `MAX(ts_ms)` sobre `samples` a 100 escrituras/s sería trabajo repetido; una
  tabla de 2–3 filas lo resuelve con actualización incremental.
* **Índice `(sensor, ts_ms)`**, que es exactamente la forma en que se consulta:
  «la serie de este sensor en esta ventana». Sin él, cada petición del
  histórico sería un escaneo completo. El índice adicional `(ts_ms)` sirve al
  trabajo por tiempo de la retención (purga por edad, techo de filas, MIN/MAX
  de marcas), que no filtra por sensor.
* **`samples_agg`** es la tabla de agregados por bucket (sección 7). No
  sustituye a `samples`: convive con ella y conserva las tendencias cuando el
  detalle se reduce. Sus claves son `(bucket_ts_ms, sensor, metric)`, una fila
  por métrica, de modo que añadir una métrica agregada no cambia el esquema.

## 5. Escalado

### 5.1 Más sensores en el mismo nodo

Es el escalado más inmediato y está contemplado:

1. **Protocolo**: añadir un prefijo de línea en el contrato (tabla 2.1) y su
   formato. El parser se amplía con un caso más; su regla de descarte de
   líneas desconocidas hace que añadir un sensor al firmware no rompa un
   servidor antiguo.
2. **Métricas**: declarar la métrica en `derived.py` con su unidad y el sensor
   al que aplica. El contrato ya distingue métricas por sensor
   (`roll/pitch/yaw` solo del MPU6050; `vib_rms`/`vib_peak` de acelerómetros).
3. **Catálogo**: exponerlo en `GET /api/sensors`, que es como el frontend
   descubre qué graficar. Con el catálogo, añadir un sensor no exige tocar el
   dashboard salvo que se quiera un panel específico.
4. **Esquema**: si la magnitud cabe en `temp_c` o en una columna existente se
   reutiliza; si no (peso del HX711, velocidad del viento), la fila sigue
   pudiendo viajar completa en `payload`, y la consulta puntual se resuelve con
   una tabla derivada `readings(sensor, ts_ms, name, value, unit)`. Esta
   segunda tabla es la ruta prevista para sensores heterogéneos sin alterar la
   tabla de IMU.

El límite práctico del nodo actual es el propio bus: los sensores I2C comparten
direcciones y tiempo de transacción, y el presupuesto del bucle ya está
consumido por el periodo de 20 ms que exige el MPU6050 a 50 Hz.

### 5.2 Más nodos (red de sensores)

La WSN de la tesis apunta a varios nodos sobre la torre. Los cambios previstos:

* **Identidad**: añadir `node_id` a `samples`, `events` y al mensaje `sample`.
  Es un campo más en el contrato; el dashboard lo convierte en un selector.
* **Reloj**: la sincronización por llegada deja de bastar. Cada nodo debe
  aportar `ts_ms` propio (RTC o NTP cuando haya red) y el servidor debe
  registrar también `received_ms`, para poder medir el desfase y corregirlo.
* **Enlace**: el camino de campo es `POST /api/ingest` desde una pasarela por
  nodo, o una cola (MQTT/LoRa) que vuelque al mismo endpoint. El contrato HTTP
  es el punto de convergencia, de modo que el transporte puede cambiar sin
  tocar el procesamiento.
* **Aislamiento de fallos**: un nodo caído debe ser un `sensor_status` con
  `present = 0`, no un servidor caído. La regla de watchdog de 2 s se aplica
  por nodo.

### 5.3 Más volumen

Cuentas de servilleta útiles para justificar cuándo el diseño actual se queda
corto:

| Magnitud | Valor |
|---|---|
| Muestras/s (2 sensores × 50 Hz) | 100 |
| Muestras/día | 8,64 × 10⁶ |
| Tamaño estimado por fila (con `payload`) | ~150–300 B |
| Crecimiento diario del archivo | ~1,5–2,5 GB/día |

Conclusión operativa: SQLite aguanta días o semanas de un nodo sin apuros
siempre que se consulte por ventana e índice; **no** es el almacén de meses de
una red de nodos. La ruta de crecimiento, en orden:

1. **Retención y agregados** (ya implementado, sección 7): tabla `samples_agg`
   y purga/reducción de la tabla fina. La agregación se hace en el
   repositorio, sin tocar el resto.
2. **Particionado temporal** (si se migra a PostgreSQL): particiones por día
   sobre `ts_ms`.
3. **Almacén de series temporales**: InfluxDB o TimescaleDB cuando concurran
   varias de las señales de la tabla 2.2 (varios nodos, meses de retención,
   agregados continuos, alta disponibilidad). El interfaz `Repository` es el
   que hace esta migración mecánica.
4. **Escalado horizontal de la entrega**: si los clientes son muchos, el
   *broadcast* del WebSocket se apoya en un bus (Redis pub/sub) y el servidor
   de API se multiplica. Para la escala de la tesis (pocos clientes en red
   local) el *broadcast* en memoria es la solución correcta por simple.

### 5.4 Más clientes

El WebSocket hace *broadcast* de cada muestra a todos los clientes: N clientes
no multiplican la ingesta, solo la salida. La ventana por defecto de
`/api/history` (5000 puntos) y el límite `limit` protegen al servidor de un
cliente que pida «todo el histórico de golpe».

## 6. Límites conocidos

Se enumeran explícitamente porque forman parte del resultado: una tesis que
declara sus límites es más útil que una que los esconde.

* **Sin autenticación ni TLS.** El servidor escucha en `127.0.0.1` y se asume
  uso en red local de confianza. Para despliegue real haría falta HTTPS/WSS,
  tokens por nodo y control de acceso a la exportación.
* **Un solo nodo de telemetría.** El esquema y el estado en memoria no
  distinguen emisores. La ampliación está prevista (sección 5.2) pero no
  implementada.
* **Muestreo sincronizado por llegada.** `ts_ms` es la hora de recepción salvo
  que el emisor envíe la suya. Válido para visualización y para un nodo;
  insuficiente para análisis modal entre nodos, donde haría falta sincronismo
  real (NTP/PTP/RTC).
* **Un escritor en la base de datos.** SQLite serializa escrituras; varios
  procesos ingiriendo a la vez requerirían otra tecnología.
* **Sin alertas ni notificaciones.** El WebSocket está preparado para ellas
  (canal bidireccional), pero la política de umbrales pertenece al capítulo de
  detección de anomalías.
* **Sin persistencia del estado derivado.** `vib_rms`, `roll`, etc. se
  recalculan al servir. La agregación (`samples_agg`) solo materializa las
  métricas crudas; si se necesitaran tendencias de métricas derivadas,
  habría que extender la agregación para calcularlas.
* **El histórico detallado es finito.** La política de retención (sección 7)
  reduce el detalle a los últimos `min(AGGREGATE_AFTER_DAYS, ventana del
  techo de filas)` días; más allá solo quedan los agregados de `samples_agg`,
  que hoy no se sirven por `/api/history`. Es un límite deliberado: acotar el
  disco a cambio de resolución histórica.
* **El frontend es una sola página.** No hay multiusuario, roles ni
  personalización de paneles.

## 7. Retención, agregación y crecimiento del almacén

### 7.1 El problema, con números medidos

La tabla `samples` crece a la tasa de muestreo y nada la frena. Cifras del
despliegue real (medidas sobre `data/monitoreo.db`, oct-2026; el archivo crece
con cada campaña de captura, pero el tamaño por fila se mantiene):

| Magnitud | Valor |
|---|---|
| Tamaño del archivo | 153.579.520 B (≈ 146,5 MiB) |
| Muestras almacenadas | 559.189 |
| Tamaño real por fila (archivo / filas, con `payload` e índices) | ≈ 275 B (274,6 B medidos) |
| Tasa de diseño (2 sensores × 50 Hz) | 100 muestras/s |
| Filas por día | 8.640.000 |
| Crecimiento diario | 8,64 M × 275 B ≈ **2,4 GB/día** |
| Crecimiento mensual | ≈ 65 GB/mes |
| Crecimiento anual | ≈ **780–870 GB/año** |

Operando 24/7 en la estación base de Río Santiago, el disco se llena en
semanas y un archivo SQLite de cientos de gigabytes degrada además cada
consulta. Sin política de retención el sistema no es desplegable.

### 7.2 Los tres mecanismos de la política

Implementados en `server/retention.py` (orquestación) sobre `server/db.py`
(SQL), configurables por bandera de `run.py` y por variables de entorno
`MONITOREO_*` (definiciones y cálculos en `server/config.py`):

1. **Agregación histórica** (`AGGREGATE_AFTER_DAYS = 7` días,
   `AGGREGATE_BUCKET_MIN = 5` min): el detalle con más de 7 días se reduce a
   `samples_agg` — promedio, mínimo, máximo y cuenta por bucket de 5 minutos
   y por métrica — y **se borra de `samples` en la misma transacción**. Nada
   se elimina sin agregarse antes: el corte de cada pasada es el más
   agresivo de los tres límites, de modo que también se agrega lo que va a
   morir por techo de filas.
2. **Retención por edad** (`RETENTION_DAYS = 30` días): red de seguridad que
   garantiza que ninguna muestra supere los 30 días aunque la agregación
   falle o esté mal configurada (p. ej. `AGGREGATE_AFTER_DAYS >
   RETENTION_DAYS`). En régimen normal la agregación ya se llevó el detalle
   a los 7 días y este paso no borra nada.
3. **Techo de filas** (`RETENTION_MAX_ROWS = 5.000.000`): si `samples`
   supera el techo se expulsan las más antiguas aunque no tengan la edad.
   Es el límite que manda a la tasa de diseño: 5 M filas ÷ 100 filas/s ≈
   50.000 s ≈ **13,9 h de detalle**, y ≈ 5 M × 275 B ≈ **1,3 GB** de archivo
   máximo. Protege contra un sensor más rápido de lo previsto, una fuga de
   datos o una reinyección masiva de logs.

Equilibrio resultante en régimen estacionario: **detalle acotado a ~1,3 GB**
(5 M filas o 7 días, lo que llegue antes) más agregados que crecen poco
(§7.3). Es decir, del orden de **2 GB estables frente a 780 GB/año** sin
política.

### 7.3 Tamaño de la agregación histórica

El detalle muere pero la tendencia se conserva. Cálculo del ocupante de
`samples_agg`:

```
buckets:  1.440 min/día ÷ 5 min        = 288 buckets/día
series:   288 × 2 sensores × 10 métricas = 5.760 filas/día   (peor caso)
          288 × (7 + 3) series = 2.880 filas/día             (implementado)
fila:     bucket_ts_ms + sensor + metric + n + avg/min/max ≈ 100–250 B
```

* Peor caso (10 métricas por sensor): 5.760 filas/día × 250 B ≈ **1,4 MB/día
  ≈ 0,5 GB/año**.
* Implementación real (7 métricas del MPU6050 + 3 del ADXL345 = 10 series
  por bucket): 2.880 filas/día ≈ **0,7 MB/día ≈ 0,25 GB/año**.

Frente a los **~780 GB/año** del detalle sin agregar, la reducción es de un
factor de ~1.500–3.000x y un año completo de tendencias cabe en una fracción
de disco. La resolución temporal resultante (5 min) es compatible con el
análisis estructural de la torre: los modos de interés son de segundos, pero
la deriva de orientación, la temperatura y el nivel de vibración de fondo se
siguen viendo por encima de la ventana de un día. Si hiciera falta más
resolución histórica, bajar `AGGREGATE_BUCKET_MIN` (a 1 min serían ~1,3
GB/año en el peor caso, todavía despreciable).

Las métricas agregadas son las crudas (`accel_*`, `gyro_*`, `temp_c`); las
derivadas (`vib_rms`, `roll`…) viven solo en memoria (sección 6) y no se
persisten, lo que se declara como límite conocido.

### 7.4 Ejecución: tarea de fondo, sin bloquear la ingesta

* La política corre como **tarea asyncio** (`retention.run_loop`) que ejecuta
  una pasada al arrancar y después cada `RETENTION_INTERVAL_S = 3600` s. El
  trabajo de SQLite es bloqueante y se ejecuta en un hilo del pool
  (`asyncio.to_thread`): el event loop sigue sirviendo WebSocket y HTTP
  mientras se purga (criterio de calidad del contrato §6).
* Las transacciones van **por tandas**: la agregación procesa tandas de 12
  buckets (1 h de datos) y la purga por edad borra tandas de 50.000 filas,
  liberando el cerrojo de escritura entre tandas. La ingesta a 50 Hz
  (thread-local + `threading.Lock` de escritura, ver `db.py`) sigue
  insertando mientras tanto; lo que llegue a un bucket ya agregado se funde
  con su agregado en la siguiente pasada (el upsert de `samples_agg` fusiona
  n/avg/min/max, no sobrescribe).
* **Tolerancia a fallos**: cada paso se protege con `try/except` y solo deja
  un log; una purga fallida jamás tumba la ingesta (contrato §6) ni el resto
  de la pasada. La política es reejecutable: no guarda estado que corromper.
* Las pruebas de `tests/test_retention.py` fijan este comportamiento,
  incluida la ingesta concurrente durante la purga.

### 7.5 Devolver espacio al sistema de archivos (VACUUM)

Los `DELETE` dejan páginas como *reutilizables*: el archivo no se encoge solo.
Dos vías, ambas prudentes con las escrituras:

* **`PRAGMA incremental_vacuum`** (cada `MONITOREO_VACUUM_INTERVAL_S`, 24 h
  por defecto): retira páginas libres del final del archivo por tandas,
  reteniendo el cerrojo de escritura solo por tanda. **Solo funciona si la
  base se creó con `auto_vacuum=INCREMENTAL`**, que `db.py` activa
  automáticamente en bases nuevas. En bases antiguas (como la actual
  `monitoreo.db`, creada antes de esta política) es un no-op: las páginas
  libres se reutilizan pero no se devuelven al SO, y el log lo indica.
* **`VACUUM` completo**, opt-in (`--vacuum-full` /
  `MONITOREO_VACUUM_FULL=1`): copia la base entera a su tamaño mínimo, pero
  **bloquea todas las escrituras durante la copia** y necesita hasta 2× el
  tamaño del archivo en disco libre. Solo en ventana de mantenimiento o con
  la ingesta parada. Es la forma de comprimir una base ya existente (y de
  migrarla a `auto_vacuum=INCREMENTAL`, que queda activo tras el VACUUM).

### 7.6 Visibilidad: `GET /api/db/stats`

```json
{"file_bytes": 153579520, "wal_bytes": 0, "samples_rows": 559189,
 "samples_agg_rows": 0, "oldest_ts_ms": ..., "newest_ts_ms": ...,
 "retention_days": 30, "max_rows": 5000000, "aggregate_after_days": 7,
 "aggregate_bucket_min": 5, "retention_interval_s": 3600,
 "est_daily_bytes": 131625620, "next_purge_ts_ms": ...,
 "freelist_pages": 0, "freelist_bytes": 0, "last_purge": {...}}
```

Es la respuesta a «¿dónde están mis datos y a qué ritmo crecen?» sin abrir
SQLite a mano: filas de detalle y de agregados, ventana temporal cubierta,
tamaño en disco (incluido el WAL y las páginas libres), estimación de
crecimiento diario (filas de la última ventana extrapoladas a 24 h al tamaño
medio por fila observado; es una pendiente orientativa, no una factura — el
valor de ejemplo, ~132 MB/día, refleja la tasa de captura de la base medida,
inferior a la tasa de diseño de 2,4 GB/día de §7.1) y la
programación/resumen de la retención. No rompe ninguna ruta existente:
`/api/history` conserva exactamente su forma (detalle crudo, con la ventana
de detalle de la política) y esta es una adición.

### 7.7 Por qué retención + agregación y no InfluxDB directamente

La sección 2.2 ya justifica SQLite frente a InfluxDB; la cuestión aquí es si,
siendo el crecimiento el problema, no habría sido mejor empezar por una base
de series temporales. La respuesta sigue siendo no, por números:

* **InfluxDB no regala la solución: la obliga.** Un bucket de InfluxDB sin
  política de retención ni *downsampling* crece los mismos ~780 GB/año. Para
  acotarlo hay que declarar `retention policies` y *tasks* de agregación
  (equivalente a lo implementado aquí), es decir, diseñar exactamente esta
  misma política en su lenguaje. El trabajo conceptual es idéntico; lo que
  cambia es dónde se escribe.
* **El coste operativo en campo.** InfluxDB es un servicio más (frecuentemente
  Docker), con su autenticación, su configuración de buckets, su copia de
  seguridad y su propio lenguaje de consulta. En un despliegue rural sin red
  de confianza, cada demonio es una causa de fallo añadida. La solución
  actual cabe en el mismo proceso Python y en un archivo.
* **La escala no lo exige.** Con la política, el estado estacionario son
  ~2 GB (detalle acotado a 5 M filas + agregados) y ~100 escrituras/s, muy
  por debajo del umbral donde SQLite sufre (un solo escritor, consultas por
  ventana indexada). El coste de oportunidad de no usar InfluxDB es nulo a
  esta escala.
* **Reversibilidad garantizada.** Todo acceso pasa por `Repository`
  (`db.py`); la retención vive en ese mismo interfaz. La migración a InfluxDB
  o TimescaleDB es reescribir `Repository` — incluidas `downsample_before`,
  `purge_older_than` y `stats` — sin tocar parser, derivadas, API ni frontend.

**Punto de migración** (señales concretas, no cronograma):

| Señal | Motivo |
|---|---|
| Varios nodos escribiendo a la vez | SQLite serializa escritores; InfluxDB/TimescaleDB absorben concurrencia |
| Consultas agregadas continuas sobre todo el histórico (RMS por hora de meses) | las *continuous queries* / *materialized views* de una TSDB hacen en la base lo que hoy hace `samples_agg` por lotes |
| Retención de agregados a años con más métricas y más nodos | el volumen de `samples_agg` deja de ser despreciable y conviene compresión y particionado nativos |
| Alta disponibilidad o replicación del almacén | fuera del alcance de un archivo SQLite |

Mientras ninguna de esas señales aparezca, retención + agregación sobre
SQLite es la solución correcta: acota el disco con números (§7.2–§7.3),
conserva las tendencias para la tesis y mantiene la reproducibilidad de
«clonar y ejecutar».

## 8. Relación con el resto del repositorio

| Ruta | Papel |
|---|---|
| `src/main.cpp` | firmware del ESP32 (PlatformIO): muestreo, validación de rango, emisión del protocolo |
| `processing/MPU6050_Visualizer/` | visualizador 3D (Processing) con el mismo filtro complementario, α = 0.96 |
| `dashboard/CONTRATO_DE_DATOS.md` | especificación normativa del protocolo |
| `dashboard/server/` | backend Python: ingesta, parseo, métricas, repositorio, API |
| `dashboard/web/` | frontend estático |
| `dashboard/data/` | base de datos SQLite (se crea al arrancar) |

El visor 3D de Processing y el dashboard comparten por diseño el filtro
complementario y la calibración del bias del giroscopio descrita en
`../processing/README.md`, de modo que ambas representaciones de la orientación
coincidan y una sirva de contraste de la otra.
