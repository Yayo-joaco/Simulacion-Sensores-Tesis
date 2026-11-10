# Plataforma de monitoreo estructural — Guía de uso

Sistema de adquisición, procesamiento y visualización de telemetría estructural
para una torre de estación base de red troncal rural. Un nodo sensor (ESP32 con
acelerómetros, giroscopio, celda de carga y sensores ambientales) emite
telemetría; un servidor local la parsea, calcula métricas derivadas, la
persiste en SQLite y la sirve en tiempo real a un dashboard web.

Tesis: *Diseño e implementación de un sistema IoT-WSN y CCTV para el monitoreo
de subsistemas críticos en un tramo de red troncal rural*.

**Documentación relacionada**

| Documento | Contenido |
|---|---|
| [`CONTRATO_DE_DATOS.md`](CONTRATO_DE_DATOS.md) | Origen de verdad del protocolo: líneas serie, rutas, esquema SQL, unidades |
| [`ARQUITECTURA.md`](ARQUITECTURA.md) | Arquitectura por capas, justificación tecnológica, escalado |
| [`TESIS_CAPITULO.md`](TESIS_CAPITULO.md) | Texto listo para el capítulo de la tesis |
| [`../processing/README.md`](../processing/README.md) | Visualizador 3D de Processing y calibración del giroscopio |

---

## 1. Qué es

La plataforma cubre el tramo **sensor → dato consultable** de un sistema de
monitoreo estructural:

```
ESP32 + sensores ──USB serie──► Servidor FastAPI ──WebSocket/REST──► Dashboard
       (50 Hz)                   · parseo          (tiempo real       (HTML/CSS/JS
                                 · métricas         e histórico)      sin build)
                                 · SQLite
```

Diseño deliberado, pensado para campo y laboratorio:

* **Doble camino de ingesta**: USB serie para pruebas con el ESP32 cableado y
  `POST /api/ingest` para despliegues sin cable. El dashboard no distingue por
  dónde llegaron los datos.
* **Todo local**: ninguna ruta del servidor ni del frontend necesita Internet.
  En el tramo rural objeto de estudio la conectividad es escasa, así que la
  plataforma no puede depender de servicios externos (ni CDN, ni fuentes web,
  ni APIs de mapas).
* **Protocolo escrito primero**: todo cambio de protocolo se documenta en
  `CONTRATO_DE_DATOS.md` antes de tocar código.

## 2. Requisitos

| Componente | Versión / valor | Nota |
|---|---|---|
| Python | 3.13 | ya creado en `dashboard/.venv` |
| Paquetes | fastapi 0.162, uvicorn 0.54, websockets 17.3, pyserial 3.5 | ver instalación |
| Navegador | cualquiera con WebSocket | sin plugins |
| ESP32 | `esp32dev` + MPU6050 (y opcionalmente ADXL345, HX711, DS18B20) | solo para modo real |
| Puerto serie | `/dev/cu.usbserial-0001`, 115200 8N1 | solo para modo real |

No se necesita Node.js, npm ni ningún proceso de compilación: el frontend es
HTML/CSS/JS servido tal cual por el propio servidor.

## 3. Instalación

El entorno ya está creado. Si hay que reproducirlo en otra máquina:

```bash
cd "/Users/ofeliacallirgos/Documents/PlatformIO/Projects/Simulacion Tesis Sensores/dashboard"

# Crear el entorno (solo si no existe .venv)
python3 -m venv .venv

# Instalar dependencias (hay conexión la primera vez)
.venv/bin/pip install fastapi uvicorn websockets pyserial
```

Usa **siempre** el Python del entorno (`.venv/bin/python`), nunca el `python3`
del sistema: así se garantiza que FastAPI, Uvicorn y PySerial sean las versiones
probadas.

## 4. Arranque

Dos modos, mismo dashboard. El servidor escucha en `http://127.0.0.1:8000`.

### 4.1 Con ESP32 real (ingesta por USB serie)

1. Sube el firmware con PlatformIO y **cierra el Monitor Serial**: solo un
   programa puede tener abierto el puerto a la vez.
2. Conecta el ESP32 por USB y comprueba el nombre del puerto:

   ```bash
   ls /dev/cu.*                  # en macOS; en Linux: ls /dev/ttyUSB*
   .venv/bin/pyserial-ports      # alternativa multiplataforma
   ```

   El firmware y `platformio.ini` asumen `/dev/cu.usbserial-0001`. Si el tuyo
   tiene otro nombre, pásalo con `--serial-port`.
3. Arranca el servidor:

   ```bash
   cd "/Users/ofeliacallirgos/Documents/PlatformIO/Projects/Simulacion Tesis Sensores/dashboard"
   .venv/bin/python server/main.py --serial-port /dev/cu.usbserial-0001
   ```
4. Abre `http://127.0.0.1:8000` en el navegador. La cabecera pasa a
   **conectado** cuando llegan muestras válidas.

El ESP32 puede reiniciarse en caliente sin tumbar el servidor: se dejan de
recibir muestras, el estado pasa a `connected = false` y se recupera solo al
volver a emitir.

### 4.2 Sin hardware (`--simulate`)

Genera telemetría sintética con el mismo formato serie que el ESP32
(`DATA,…` a 50 Hz y eventos `[SENSOR] …`), útil para desarrollar el dashboard,
hacer demostraciones o ensayar la detección de anomalías sin la electrónica
delante:

```bash
cd "/Users/ofeliacallirgos/Documents/PlatformIO/Projects/Simulacion Tesis Sensores/dashboard"
.venv/bin/python server/main.py --simulate
```

En este modo no se abre ningún puerto serie. El dashboard, la API, la base de
datos y el WebSocket se comportan exactamente igual que con el hardware.

> **Convención de comandos:** los ejemplos asumen el punto de entrada
> `server/main.py` con los flags `--serial-port`, `--simulate` y `--port`
> (8000 por defecto). Si el servidor se expone en otro módulo, el patrón de
> arranque y los flags son los mismos.

## 5. Cómo leer el dashboard

La interfaz se sirve en `/` y se alimenta del WebSocket `/ws` (tiempo real) y de
`/api/history` (histórico). La información que muestra corresponde uno a uno con
los campos del contrato.

| Zona | Qué muestra | De dónde sale |
|---|---|---|
| **Cabecera de estado** | `connected`, puerto serie y, por sensor, `present`, `last_ts_ms` y `n_samples` | mensajes `status` del WebSocket |
| **Gráficas de telemetría** | `accel_x/y/z` (m/s²), `gyro_x/y/z` (rad/s) y `temp_c` (°C) en vivo | `raw.*` de los mensajes `sample` |
| **Orientación** | `roll`, `pitch`, `yaw` en grados (solo MPU6050) | `derived.*` |
| **Vibración** | `vib_rms` y `vib_peak` de la ventana de 1 s (MPU6050 y ADXL345) | `derived.*` |
| **Magnitudes** | `accel_mag` (m/s²) y `gyro_mag` (rad/s) | `derived.*` |
| **Registro de eventos** | líneas `[SENSOR] …` del nodo con su nivel (`info`/`warn`/`error`) | mensajes `log` |
| **Histórico** | ventana hacia atrás (`seconds`) y exportación | `GET /api/history`, `GET /api/export.csv` |

Criterios de lectura:

* **Unidades**: la aceleración siempre en m/s²; el giro **siempre en rad/s**
  tanto en crudo como en base de datos. Los ángulos derivados (`roll`, `pitch`,
  `yaw`) sí van en grados. La conversión a grados por segundo se hace al
  presentar, nunca al almacenar.
* **`vib_rms` es la señal de referencia para umbrales**: es la raíz cuadrática
  media de la aceleración de vibración (con la gravedad retirada) en la ventana
  de 1 s. Es la magnitud sobre la que se fijarán los umbrales de detección
  estructural.
* **`yaw` deriva**: se integra desde el giroscopio (con bias calibrado), de modo
  que es fiable para cambios relativos, no para rumbo absoluto.
* **`connected = false`** significa que hace más de 2 s que no llega ninguna
  muestra válida. No es un error del servidor: es la señal de que el nodo o el
  enlace están caídos.
* **Contadores**: `n_samples` crece con cada muestra persistida; un reinicio
  del ESP32 no lo resetea, pero sí hace que `last_ts_ms` se detenga hasta que
  vuelva a llegar telemetría.

## 6. Referencia rápida de la API

Servidor: `http://127.0.0.1:8000` (detalle completo en `CONTRATO_DE_DATOS.md`).

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` | página del dashboard |
| `GET` | `/api/status` | estado de ingesta y de cada sensor |
| `GET` | `/api/sensors` | catálogo de sensores y métricas disponibles |
| `GET` | `/api/history` | series históricas (`sensor`, `metrics`, `seconds`, `limit`) |
| `GET` | `/api/export.csv` | descarga CSV para análisis posterior |
| `POST` | `/api/ingest` | ingesta alternativa por red |
| `GET` | `/api/health` | comprobación de vida |
| `WS` | `/ws` | mensajes `sample`, `status` y `log` en tiempo real |

Ejemplo de ingesta por HTTP (el camino de campo, sin cable):

```bash
curl -X POST http://127.0.0.1:8000/api/ingest \
  -H 'Content-Type: application/json' \
  -d '{"sensor":"mpu6050","accel_x":0.12,"accel_y":-0.04,"accel_z":9.80,
       "gyro_x":0.001,"gyro_y":0.0,"gyro_z":0.0,"temp_c":26.3}'
```

## 7. Solución de problemas

**El servidor no puede abrir el puerto serie.**
Casi siempre es que otro programa lo tiene tomado: cierra el Monitor Serial de
PlatformIO, `screen`, `miniterm` o cualquier IDE con el monitor abierto, y
vuelve a intentarlo. Comprueba también el nombre del puerto con
`.venv/bin/pyserial-ports`. Mientras tanto puedes arrancar con `--simulate` o
usar `POST /api/ingest`, que no necesita serie.

**El dashboard dice `connected = false`.**
No llegan muestras válidas desde hace más de 2 s. Comprueba en orden: (1) que el
ESP32 está alimentado y emitiendo; (2) que el firmware se subió bien; (3) el
cable USB; (4) el registro de eventos del dashboard, donde el propio nodo
reporta sus fallos.

**Aparecen eventos `[MPU6050] I2C ERROR` o `MUESTRAS INVALIDAS`.**
Son dos averías distintas y el firmware protege el sistema en ambos casos: es
preferible no emitir nada a emitir datos falsos, y mientras el error persiste
no hay gráficas de ese sensor (el comportamiento correcto).

* `[MPU6050] I2C ERROR - sin respuesta en el bus`: el chip **dejó de
  responder** al ping. Suele ser un cable desconectado o el sensor sin
  alimentación.
* `[MPU6050] MUESTRAS INVALIDAS` con el sensor detectado: el chip responde
  (apareció en el escaneo `[SCAN]` del arranque y sigue contestando al ping),
  pero sus **datos llegan corruptos** y el validador de rangos físicos los
  descarta. Que el LED del módulo esté encendido solo prueba VCC y GND: los
  datos viajan por SDA/SCL y ahí está el fallo.

El mensaje lleva las magnitudes de la muestra descartada
(`|a|=… m/s2 |w|=… rad/s`), que identifican el origen:

| Magnitud observada | Causa probable |
|---|---|
| `\|a\| = 0.000` o `nan` | transferencias I2C fallando: cables SDA/SCL/GND sueltos o largos |
| `\|a\|` en cientos de m/s² | basura pura del bus: mismo origen |
| `\|g\| ≥ 12 rad/s` con `\|a\|` normal | el acelerómetro llega bien pero el giro no: cable frío o módulo dañado |

Lista de comprobación en orden: (1) SDA→GPIO21 y SCL→GPIO22, sin intercambiar;
(2) GND común entre el módulo y el ESP32; (3) VCC a 3.3 V; (4) cables Dupont
cortos y bien fijados; (5) AD0 a GND si es un módulo desnudo; (6) desconectar
temporalmente el ADXL345 —comparte el bus I2C— para aislar si la degradación
viene de él. Las repeticiones del mismo error se agregan en la consola como
`(×N repeticiones)` para no inundarla.

**Veo líneas raras o texto de arranque en el monitor.**
El ESP32 imprime el bootloader a 74880 baudios y un banner de arranque antes de
la telemetría. El parser descarta todo lo que no empiece con `DATA,`, `ADXL,` ni
`[`, así que esos restos no entran en la base de datos. Si aparecen líneas
malformadas, se cuentan en `parse_errors` y la ingesta continúa.

**El histórico sale vacío.**
Comprueba la ventana `seconds` de la consulta (por defecto 60 s) y que haya
muestras con `GET /api/status`. En modo simulado el histórico empieza a llenarse
desde que arrancaste el servidor.

**El giro o el yaw no coinciden con el visor 3D de Processing.**
Ambos usan el mismo filtro complementario (α = 0.96) y la misma convención de
ejes, pero el bias del giroscopio se calibra en cada arranque: mantén la placa
en reposo los primeros segundos (ver `../processing/README.md`).

**El archivo `data/monitoreo.db` está ocupado o da errores de bloqueo.**
SQLite permite un solo escritor. No abras la base de datos con otra herramienta
en modo escritura mientras el servidor corre. Para analizar datos en frío, usa
`GET /api/export.csv` y trabaja sobre la copia.

## 8. Criterios de calidad asumidos

Los siguientes compromisos forman parte del diseño (sección 6 del contrato) y
son los que debe cumplir cualquier cambio futuro sobre la plataforma:

* La ingesta de una muestra a 50 Hz **no debe bloquear** la respuesta HTTP.
* El dashboard debe seguir dibujando a 60 fps con 2 sensores a 50 Hz.
* Un reinicio del ESP32 no debe tumbar el servidor: se pierden muestras, se
  marca `connected = false` y se recupera solo.
* Ninguna ruta del servidor depende de Internet: todo funciona en local.

## 9. Límites conocidos

La plataforma es una prueba de concepto de tesis a escala de laboratorio y lo
dice con honestidad: **no hay autenticación ni TLS** (solo escucha en
`127.0.0.1`), soporta **un solo nodo** de telemetría y el muestreo se
**sincroniza por llegada** (marca de tiempo de recepción salvo que el emisor
envíe `ts_ms`). El detalle y el análisis de estas limitaciones están en
`ARQUITECTURA.md` y `TESIS_CAPITULO.md`.
