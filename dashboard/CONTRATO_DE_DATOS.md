# Contrato de datos — Plataforma de monitoreo estructural

Documento de diseño. Lo usan como referencia el firmware del ESP32, el
servidor de ingesta y el dashboard web. Todo cambio de protocolo se documenta
aquí primero.

Tesis: *Diseño e implementación de un sistema IoT-WSN y CCTV para el monitoreo
de subsistemas críticos en un tramo de red troncal rural*.

---

## 1. Arquitectura

```
┌─────────────────────┐   115200 8N1    ┌──────────────────────────┐
│  ESP32 + sensores   │ ──────────────► │  Servidor de ingesta     │
│  MPU6050, ADXL345,  │   (USB serie)   │  Python / FastAPI        │
│  HX711, DS18B20 ... │                 │                          │
└─────────────────────┘                 │  · parsea protocolo      │
                                        │  · métricas derivadas    │
┌─────────────────────┐    Wi-Fi/MQTT   │  · persiste en SQLite    │
│  ESP32 (futuro      │ ──────────────► │                          │
│  despliegue rural)  │   (ingest HTTP) └───────────┬──────────────┘
└─────────────────────┘                             │
                                        ┌───────────┴──────────────┐
                                        │  WebSocket  /ws          │
                                        │  REST       /api/*       │
                                        └───────────┬──────────────┘
                                                    │
                                        ┌───────────▼──────────────┐
                                        │  Dashboard web           │
                                        │  tiempo real + histórico │
                                        └──────────────────────────┘
```

Dos caminos de ingesta, mismo destino:

| Camino | Uso | Estado |
|---|---|---|
| USB serie → `ingest.py` | pruebas de laboratorio con el ESP32 cableado | **activo** |
| HTTP `POST /api/ingest` | despliegue sin cable (Wi-Fi / pasarela) | **activo** |

El dashboard no sabe por qué camino llegaron los datos.

---

## 2. Protocolo serie (ESP32 → servidor)

Líneas UTF-8 terminadas en `\n`, 115200 baudios.

### 2.1 Muestras de telemetría

| Prefijo | Formato | Sensor | Cadencia | Unidades |
|---|---|---|---|---|
| `DATA,` | `DATA,ax,ay,az,gx,gy,gz,temp` | MPU6050 | 50 Hz | m/s², rad/s, °C |
| `ADXL,` | `ADXL,ax,ay,az` | ADXL345 | 50 Hz | m/s² |

Ejemplo real:

```text
DATA,0.121,-0.043,9.802,0.00123,-0.00045,0.00012,26.31
ADXL,0.020,-0.011,9.798
```

Reglas de formato:

* Punto decimal, sin coma como separador decimal.
* `%.3f` para aceleración, `%.5f` para giro, `%.2f` para temperatura.
* Si un valor no es un número finito, **toda la línea se descarta**.

### 2.2 Diagnóstico humano

Cualquier línea que empiece con `[` es texto legible y se registra como
*evento*, no como muestra. Ejemplos:

```text
[MPU6050] OK - Detectado en 0x68
[MPU6050] I2C ERROR - sin respuesta en el bus
[DS18B20] Temperatura: 28.44 °C
```

### 2.3 Robustez del parser

* Se ignoran líneas que no empiecen con `DATA,`, `ADXL,` ni `[`.
  Esto descarta los restos del bootloader (74880 baudios) y el banner de arranque.
* Una línea mal formada no interrumpe la ingesta: se cuenta como `parse_errors`
  y se continúa.
* El servidor marca `connected = false` si no llega ninguna muestra válida
  durante **2 segundos**.

---

## 3. Modelo de datos (SQLite)

Base de datos local, un solo archivo `data/monitoreo.db`. Cero configuración,
portable, exportable. El acceso va por una capa de repositorio
(`db.py` → clase `Repository`) para poder migrar a InfluxDB/PostgreSQL sin
tocar el resto del sistema.

```sql
CREATE TABLE IF NOT EXISTS samples (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms     INTEGER NOT NULL,          -- epoch UTC en milisegundos
  sensor    TEXT    NOT NULL,          -- 'mpu6050' | 'adxl345' | ...
  accel_x   REAL, accel_y REAL, accel_z REAL,
  gyro_x    REAL, gyro_y  REAL, gyro_z REAL,
  temp_c    REAL,
  payload   TEXT                       -- JSON crudo, para auditoría
);
CREATE INDEX IF NOT EXISTS idx_samples_sensor_ts ON samples(sensor, ts_ms);

CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms     INTEGER NOT NULL,
  sensor    TEXT,
  level     TEXT NOT NULL,             -- 'info' | 'warn' | 'error'
  code      TEXT,
  message   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sensor_status (
  sensor     TEXT PRIMARY KEY,
  present    INTEGER NOT NULL DEFAULT 0,
  last_ts_ms INTEGER,
  n_samples  INTEGER NOT NULL DEFAULT 0
);
```

Convención: las columnas `gyro_*` siempre en **rad/s** en base de datos. La
conversión a grados se hace al presentar. La aceleración siempre en **m/s²**.

---

## 4. API HTTP + WebSocket

Servidor: `http://127.0.0.1:8000`

### 4.1 REST

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` | página del dashboard |
| `GET` | `/api/status` | estado de ingesta y de cada sensor |
| `GET` | `/api/sensors` | catálogo de sensores y métricas disponibles |
| `GET` | `/api/history` | series históricas (ver parámetros) |
| `GET` | `/api/export.csv` | descarga CSV para análisis posterior |
| `POST` | `/api/ingest` | ingesta alternativa por red |
| `GET` | `/api/health` | comprobación de vida |

Parámetros de `/api/history`:

| Parámetro | Tipo | Defecto | Descripción |
|---|---|---|---|
| `sensor` | string | `mpu6050` | sensor a consultar |
| `metrics` | csv | todos | p.ej. `accel_x,gyro_z` |
| `seconds` | int | `60` | ventana hacia atrás desde ahora |
| `limit` | int | `5000` | máximo de puntos devueltos |

Respuesta de `/api/history`:

```json
{
  "sensor": "mpu6050",
  "from_ms": 1761000000000,
  "to_ms": 1761000060000,
  "count": 3000,
  "series": {
    "accel_x": [[1761000000000, 0.12], [1761000000020, 0.13]],
    "gyro_z":  [[1761000000000, 0.0012]]
  }
}
```

`POST /api/ingest` acepta:

```json
{"sensor": "mpu6050", "ts_ms": 1761000000000,
 "accel_x": 0.12, "accel_y": -0.04, "accel_z": 9.80,
 "gyro_x": 0.001, "gyro_y": 0.000, "gyro_z": 0.000,
 "temp_c": 26.3}
```

`ts_ms` es opcional; si falta, el servidor usa la hora de recepción.

### 4.2 WebSocket `ws://127.0.0.1:8000/ws`

Cada mensaje es un objeto JSON con un campo `type`:

**Muestra en vivo**

```json
{
  "type": "sample",
  "sensor": "mpu6050",
  "ts_ms": 1761000000000,
  "raw": {
    "accel_x": 0.121, "accel_y": -0.043, "accel_z": 9.802,
    "gyro_x": 0.00123, "gyro_y": -0.00045, "gyro_z": 0.00012,
    "temp_c": 26.31
  },
  "derived": {
    "roll": 0.25, "pitch": -0.71, "yaw": 12.44,
    "accel_mag": 9.803, "gyro_mag": 0.00134,
    "vib_rms": 0.021, "vib_peak": 0.043
  }
}
```

* `raw.gyro_*` en **rad/s**. `derived.roll/pitch/yaw` en **grados**.
* `derived.*` solo se envía para el sensor al que le corresponden:
  `roll/pitch/yaw` → `mpu6050`; `vib_rms/vib_peak` → `adxl345` y `mpu6050`.
* `vib_rms` es la raíz cuadrática media de la aceleración de vibración
  (aceleración con la gravedad retirada) en una ventana de **1 segundo**.

**Cambio de estado**

```json
{"type": "status", "connected": true,
 "port": "/dev/cu.usbserial-0001",
 "sensors": {"mpu6050": {"present": true, "last_ts_ms": 1761000000000,
                        "n_samples": 3021},
             "adxl345": {"present": true, "last_ts_ms": 1761000000000,
                        "n_samples": 3018}}}
```

**Evento / log**

```json
{"type": "log", "ts_ms": 1761000000000, "level": "error",
 "sensor": "mpu6050", "line": "[MPU6050] I2C ERROR - sin respuesta en el bus"}
```

El servidor emite `status` al conectar un cliente y cada vez que cambia el
estado de un sensor.

Agregación de eventos repetidos: las ráfagas de eventos idénticos (misma línea
de error decenas de veces por segundo durante una avería de bus) se publican
como un único aviso cuyo `line` lleva el sufijo `(×N repeticiones)`, con la
primera aparición siempre inmediata y sin sufijo. El nivel se infiere del
texto: `ERROR`/`FAIL` → `error`, `WARN`/`INVALID` → `warn`, resto → `info`.

---

## 5. Métricas derivadas

Se calculan en el servidor (`derived.py`), en una sola capa, para que la
visualización y la futura detección de anomalías compartan exactamente los
mismos valores.

| Métrica | Definición | Sensor |
|---|---|---|
| `accel_mag` | `√(ax²+ay²+az²)` en m/s² | todos los acelerómetros |
| `gyro_mag` | `√(gx²+gy²+gz²)` en rad/s | MPU6050 |
| `roll` | `atan2(ay, az)` en grados | MPU6050 |
| `pitch` | `atan2(-ax, √(ay²+az²))` en grados | MPU6050 |
| `yaw` | integración de `gyro_z` con bias corregido, envuelto a (-180, 180] | MPU6050 |
| `vib_rms` | RMS de `√(ax²+ay²+az²) - g` en ventana de 1 s | MPU6050, ADXL345 |
| `vib_peak` | máximo de esa magnitud en la ventana de 1 s | MPU6050, ADXL345 |

La orientación roll/pitch se mezcla con un **filtro complementario (α = 0.96)**
idéntico al del visualizador 3D en Processing, de modo que ambos visores
coincidan. Los detalles de calibración del bias del giroscopio están en
`../processing/README.md`.

`g = 9.80665 m/s²`. `vib_rms` es la magnitud que se usará para los umbrales de
vibración del capítulo de detección de anomalías.

---

## 6. Criterios de calidad

* La ingesta de una muestra a 50 Hz **no debe bloquear** la respuesta HTTP.
* El dashboard debe seguir dibujando a 60 fps con 2 sensores a 50 Hz.
* Un reinicio del ESP32 no debe tumbar el servidor: se pierden muestras, se
  marca `connected = false` y se recupera solo.
* Ninguna ruta del servidor depende de Internet: todo funciona en local.
