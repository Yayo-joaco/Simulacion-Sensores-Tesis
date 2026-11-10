# Firmware: envío de telemetría por WiFi/HTTP

Qué hay que añadir al firmware del ESP32 para pasar del USB serie a la red,
hacia el endpoint que **ya existe y ya funciona**: `POST /api/ingest`
(contrato §4.1). El diseño del despliegue completo (dónde corre el servidor,
seguridad, costes) está en [`../DESPLIEGUE_NUBE.md`](../DESPLIEGUE_NUBE.md);
aquí solo la parte del nodo.

**Estado**: este documento es el **diseño + código de referencia**. No está
probado en campo todavía (la prueba de campo es la fase 1 del plan de
despliegue). El firmware actual (`../../src/main.cpp`) sigue siendo el bueno:
muestreo, validación de rango físico y protocolo serie intactos. La propuesta
es **añadir** un módulo de uplink, no reescribir nada.

---

## 1. Qué se añade y por qué

| Módulo | Función | Por qué hace falta |
|---|---|---|
| `wifiMaintain()` | conexión WiFi con reconexión exponencial | la red rural cae y vuelve; el nodo no puede reiniciarse cada vez |
| `timeMaintain()` / `now_ms()` | NTP → epoch UTC en ms | `ts_ms` es epoch UTC (contrato §3). Sin reloj, el servidor estampa la hora de recepción y la serie se corrompe en modo cola |
| Cola circular en RAM | almacena muestras mientras no hay red | sin ella, cada segundo de red caída es un hueco irrecuperable en la tesis |
| `serializeSample()` | muestra → JSON del contrato §4.1 | el servidor solo acepta ese JSON |
| `uploadBurst()` | ráfagas de `POST` por HTTPS con keep-alive | 100 muestras/s son 100 peticiones/s; sin keep-alive y ráfagas, el RTT de 4G (30-80 ms) limita a ~15 muestras/s |
| Cabecera `Authorization: Bearer` | token por dispositivo | el servidor aún no lo valida, pero se manda ya para no tocar el firmware después (ver §6) |

Lo que **no** cambia: el protocolo serie del contrato §2. El nodo sigue
imprimiendo `DATA,` / `ADXL,` / `[..]` por USB, de modo que el camino de
laboratorio (fase 0 del plan) sigue funcionando y se puede comparar un mismo
experimento por USB y por red.

---

## 2. Presupuesto de datos (obligatorio leer antes de elegir tasa)

Cálculos detallados en [`../DESPLIEGUE_NUBE.md`](../DESPLIEGUE_NUBE.md) §1.0.
Resumen para el diseño del nodo:

| Concepto | Valor |
|---|---|
| JSON por muestra MPU6050 (7 valores + sensor + ts_ms) | ~158 B |
| JSON por muestra ADXL345 (3 valores) | ~91 B |
| Payload útil a 2×50 Hz | ~12,5 kB/s |
| Añadiendo cabeceras HTTP req+resp (~327 B) con **1 POST por muestra** | **~45 kB/s** (las cabeceras pesan más que los datos) |
| Con **lotes** (50 muestras/POST) — **ESCENARIO FUTURO**: exige un endpoint de arrays que hoy no existe (`../DESPLIEGUE_NUBE.md` §8.5) | ~13 kB/s |
| Volumen 24/7 por nodo, con el firmware actual (1 POST/muestra) | **~108-117 GB/mes** de plan de datos |
| Volumen 24/7 por nodo, en lotes (solo con el endpoint de lotes) | ~32 GB/mes |

Conclusión que condiciona el código: **se envía en ráfagas sobre una conexión
persistente**, y eso resuelve el límite del RTT (1/RTT = 12-30 muestras/s sobre
4G), pero **no** ahorra las cabeceras: se pagan en cada POST, son ~33 kB/s a
100 muestras/s. Con el firmware actual (1 POST por muestra) una campaña
continua 24/7 sobre 4G exige un plan de **~108-117 GB/mes**. Los ~32-50 GB/mes
solo son alcanzables con el endpoint de lotes (pendiente) o reduciendo tasa
(p. ej. enviar a 50 Hz solo el ADXL345 y el MPU6050 a 10 Hz en vivo). La tasa
de muestreo interna **no** se baja: lo que se reduce es lo que viaja.

---

## 3. Diseño de la cola local

| Magnitud | Cálculo | Valor |
|---|---|---|
| `Sample` empaquetado en RAM | 8 (ts_ms) + 1 + 3 padding + 7×4 B | **40 B** (verificado con `sizeof`) |
| Ritmo de captura | 100 muestras/s × 40 B | 4,0 kB/s |
| Cola de 2048 muestras **en heap** (`malloc` en `setup()`) | 2048 × 40 B | **80 KB → ~20 s** de red caída (2048 / 100 s⁻¹) |
| Heap libre típico del ESP32 con WiFi + TLS | — | 150-250 KB: de ahí 2048 y no 20000, y de ahí que la cola viva en el **heap** |
| La misma cola como array estático (`static Sample q[2048]`) | 80 KB de `.bss` en `dram0_0_seg` | **no enlaza**: desborda el DRAM estático en 3424 B (verificado con espressif32/gcc 8.4, env `esp32dev`). Máximo estático viable: 1024 muestras (~10 s) |

La asignación es **en heap** a propósito: la fila de 2048 se justifica por el
heap libre (150-250 KB), no por el DRAM estático, que no llega ni de lejos a
80 KB. El sketch de §4 hace el `malloc` en `setup()`; quien prefiera el array
estático debe bajar a `QUEUE_LEN <= 1024` y aceptar ~10 s de autonomía.

Reglas (las mismas que [`../DESPLIEGUE_NUBE.md`](../DESPLIEGUE_NUBE.md) §5.1):

1. **FIFO estricto**. Si la cola se llena, se descarta la muestra **nueva** y se
   cuenta (`dropped_samples`): se conserva el arranque del corte, que es lo que
   se analiza después.
2. **Cortes largos → SD, no flash**: 4,0 kB/s son 346 MB/día; la NVS interna del
   ESP32 no está para eso (desgaste) y además guarda el token. Con un módulo SD
   (~US$ 5) una SD de 8 GB cubre ~23 días a tasa completa, o ~7 meses si en
   modo sin red se degrada a 5 Hz (10 muestras/s × 40 B = 400 B/s = 35 MB/día). El ejemplo lleva solo la cola RAM; el
   volcado a SD es el mismo `Sample` append-and-flush.
3. **Entrega at-least-once**: si la conexión se cae a mitad de ráfaga, las
   muestras no confirmadas se reintentarán. El servidor **no** deduplica hoy:
   un reintento puede dejar muestras repetidas en la base de datos. Es un
   límite conocido y declarado (ver §6).

---

## 4. Código de referencia completo

Un único sketch (`wifi_uplink.ino`) para Arduino-ESP32 (core 2.x/3.x). Sin
dependencias externas (sin ArduinoJson): el JSON se serializa con `snprintf`.
Compilable como está para una prueba de uplink; los puntos de enganche con el
firmware real están en §5.

```cpp
/*
 * wifi_uplink.ino — Uplink WiFi/HTTPS para el nodo sensor de la tesis.
 * Tesis: IoT-WSN y CCTV para monitoreo de subsistemas criticos.
 *
 * POR QUE existe este fichero: el firmware actual solo habla USB serie
 * (CONTRATO_DE_DATOS.md seccion 2) y el servidor YA acepta la misma
 * telemetria por red en POST /api/ingest (contrato seccion 4.1). Este sketch
 * muestra el camino de red completo: WiFi con reconexion, reloj NTP para
 * ts_ms, cola local cuando no hay enlace y envio por HTTPS con keep-alive en
 * rafagas. El protocolo serie se conserva tal cual: por USB se ve lo mismo
 * que antes, para poder comparar ambos caminos.
 *
 * INSTALACION: este archivo va aparte del firmware real (src/main.cpp) a
 * proposito: la fase 1 del plan de despliegue valida primero el uplink y
 * luego se integra (ver dashboard/firmware/README.md seccion 5).
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <time.h>
#include <sys/time.h>
#include <stdlib.h>   // malloc: la cola se reserva en heap (ver abajo)

// ---------------------------------------------------------------------------
// Configuracion. En produccion: WiFi y token en NVS (Preferences) y el token
// NUNCA commiteado a git. Aqui van como constantes para que el ejemplo sea
// compilable tal cual.
// ---------------------------------------------------------------------------
static const char* WIFI_SSID     = "LAB-PUCP";          // cambiar
static const char* WIFI_PASS     = "cambiar-esta-clave"; // cambiar
static const char* SERVER_HOST   = "monitoreo.ejemplo.org"; // dominio con TLS
static const uint16_t SERVER_PORT = 443;
static const char* INGEST_PATH   = "/api/ingest";

// Token por dispositivo (cabecera Authorization: Bearer, ver DESPLIEGUE_NUBE
// seccion 3.2). El servidor hoy lo IGNORA — se envia ya para no tener que
// tocar el firmware cuando se implemente la autenticacion.
static const char* DEVICE_TOKEN = "pon-aqui-un-token-largo-y-aleatorio";

// CA raiz que firma el certificado del servidor. Para Let's Encrypt es el
// ISRG Root X1 (https://letsencrypt.org/certs/isrgrootx1.pem). Validar el
// certificado es OBLIGATORIO en campo: sin ello, cualquiera que se ponga en
// medio roba el token. setInsecure() es solo para el banco de pruebas.
static const char* TLS_ROOT_CA =
    "-----BEGIN CERTIFICATE-----\n"
    "MIIFazCCA1OgAwIBAgIRAIIQz7DSQONZRGPgu2OCiwAwDQYJKoZIhvcNAQELBQAw\n"
    "...(PEM completo del ISRG Root X1, pegar aqui)...\n"
    "-----END CERTIFICATE-----\n";

// ---------------------------------------------------------------------------
// Muestras y cola circular en RAM.
// Presupuesto (ver README seccion 3): Sample = 40 B, 100 muestras/s = 4,0 kB/s.
// 2048 muestras = 80 KB = ~20 s de red caida. Cortes largos: SD, no flash.
// POR QUE LA COLA VA EN HEAP: los 80 KB NO caben como array estatico. Un
// `static Sample queue_buf[2048]` cae en .bss y desborda el DRAM estatico
// (dram0_0_seg) en 3424 B: compila, pero NO enlaza (verificado con
// espressif32/gcc 8.4, env esp32dev). El heap con WiFi + TLS tiene 150-250 KB
// libres, asi que alla si cabe (README seccion 3).
// ---------------------------------------------------------------------------
enum SensorId : uint8_t { SENSOR_MPU6050 = 0, SENSOR_ADXL345 = 1 };

struct Sample {
  int64_t ts_ms;      // epoch UTC en ms (contrato seccion 3); 0 = sin reloj
  uint8_t sensor;     // SensorId
  uint8_t pad[3];
  float values[7];    // accel_x/y/z, gyro_x/y/z, temp_c (NaN si no aplica)
};

static const size_t QUEUE_LEN = 2048;
static Sample* queue_buf = nullptr;  // EN HEAP, reservado en setup() (ver arriba)
static size_t queue_head = 0;   // por donde se envia
static size_t queue_tail = 0;   // por donde se captura
static size_t queue_count = 0;
static uint32_t dropped_samples = 0;   // cola llena: se descarta lo nuevo
static uint32_t rejected_samples = 0;  // servidor respondio 4xx: muestra mala

// Todo ocurre en loop(), sin tareas concurrentes: no hay carreras. Si alguna
// vez se muestrea desde un timer/otra tarea FreeRTOS, proteger con portMUX.
static bool queue_push(const Sample& s) {
  // queue_buf == nullptr solo si el malloc de setup() fallo: no escribir en 0x0.
  if (queue_buf == nullptr || queue_count >= QUEUE_LEN) {
    dropped_samples++;
    return false;
  }
  queue_buf[queue_tail] = s;
  queue_tail = (queue_tail + 1) % QUEUE_LEN;
  queue_count++;
  return true;
}
static const Sample& queue_peek(size_t offset) {
  return queue_buf[(queue_head + offset) % QUEUE_LEN];
}
static void queue_pop(size_t n) {
  queue_head = (queue_head + n) % QUEUE_LEN;
  queue_count -= n;
}

// ---------------------------------------------------------------------------
// Reloj: ts_ms es epoch UTC en ms. Sin NTP no hay ts_ms fiable y el servidor
// usaria la hora de recepcion (contrato seccion 4.1: ts_ms es opcional).
// ---------------------------------------------------------------------------
static bool ntp_started = false;
static bool clock_synced() {
  // Post-2024: si time() devuelve algo menor, el reloj aun no sincronizo.
  return time(nullptr) > 1704067200LL;
}
static int64_t now_ms() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);  // hora del sistema del ESP32, ya con NTP
  return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}
static void time_maintain() {
  if (ntp_started || WiFi.status() != WL_CONNECTED) return;
  // Offset 0: el contrato trabaja en UTC, no en hora local. Dos servidores
  // NTP por redundancia.
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  ntp_started = true;
}

// ---------------------------------------------------------------------------
// WiFi con reconexion y backoff exponencial. La red del tramo rural cae: el
// bucle de muestreo NO debe enterarse mas que de un booleano.
// ---------------------------------------------------------------------------
static uint32_t wifi_next_attempt_ms = 0;
static uint32_t wifi_backoff_ms = 1000;

static void wifi_maintain() {
  if (WiFi.status() == WL_CONNECTED) {
    wifi_backoff_ms = 1000;  // recuperado: se resetea el backoff
    return;
  }
  if (millis() < wifi_next_attempt_ms) return;
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  // Backoff exponencial (1 s -> 60 s) con pequeno jitter determinista, para
  // que varios nodos no sincronicen sus reintentos y saturen el AP.
  wifi_next_attempt_ms = millis() + wifi_backoff_ms + (millis() % 250);
  wifi_backoff_ms = wifi_backoff_ms * 2;
  if (wifi_backoff_ms > 60000) wifi_backoff_ms = 60000;
}

// ---------------------------------------------------------------------------
// Serializacion al JSON del contrato seccion 4.1. Sin ArduinoJson: snprintf
// basta y no anade dependencia. Formatos identicos al contrato:
//   %.3f aceleracion, %.5f giro, %.2f temperatura.
// Si ts_ms == 0 (reloj sin sincronizar) el campo se OMITE para que el servidor
// estampe la hora de recepcion; enviar 0 corromperia la serie temporal.
// ---------------------------------------------------------------------------
static size_t serialize_sample(const Sample& s, char* out, size_t out_len) {
  const char* name = (s.sensor == SENSOR_MPU6050) ? "mpu6050" : "adxl345";
  char ts_part[32];
  if (s.ts_ms > 0) {
    snprintf(ts_part, sizeof(ts_part), ",\"ts_ms\":%lld", (long long)s.ts_ms);
  } else {
    ts_part[0] = '\0';
  }
  int n;
  if (s.sensor == SENSOR_MPU6050) {
    n = snprintf(out, out_len,
        "{\"sensor\":\"%s\"%s,"
        "\"accel_x\":%.3f,\"accel_y\":%.3f,\"accel_z\":%.3f,"
        "\"gyro_x\":%.5f,\"gyro_y\":%.5f,\"gyro_z\":%.5f,"
        "\"temp_c\":%.2f}",
        name, ts_part,
        s.values[0], s.values[1], s.values[2],
        s.values[3], s.values[4], s.values[5], s.values[6]);
  } else {
    n = snprintf(out, out_len,
        "{\"sensor\":\"%s\"%s,"
        "\"accel_x\":%.3f,\"accel_y\":%.3f,\"accel_z\":%.3f}",
        name, ts_part, s.values[0], s.values[1], s.values[2]);
  }
  if (n <= 0 || (size_t)n >= out_len) return 0;  // buffer corto: no enviar basura
  return (size_t)n;
}

// ---------------------------------------------------------------------------
// Uplink HTTPS. Una sola conexion TLS persistente; se escriben varias
// peticiones seguidas y se leen sus respuestas en el mismo orden (keep-alive +
// rafaga). Con peticion-respuesta sincronica sobre 4G (RTT 30-80 ms) el techo
// es 1/RTT = 12-30 muestras/s, insuficiente para 100 muestras/s.
// ---------------------------------------------------------------------------
static WiFiClientSecure net;

static bool ensure_tls_connected() {
  if (net.connected()) return true;
  if (WiFi.status() != WL_CONNECTED) return false;
  net.stop();
  net.setCACert(TLS_ROOT_CA);     // valida el certificado del servidor
  if (!net.connect(SERVER_HOST, SERVER_PORT)) return false;
  net.setTimeout(5000);           // ninguna lectura eterna
  return true;
}

static bool write_post_request(const Sample& s) {
  char body[224];
  size_t body_len = serialize_sample(s, body, sizeof(body));
  if (body_len == 0) return false;
  char req[512];
  int n = snprintf(req, sizeof(req),
      "POST %s HTTP/1.1\r\n"
      "Host: %s\r\n"
      "Authorization: Bearer %s\r\n"
      "Content-Type: application/json\r\n"
      "Content-Length: %u\r\n"
      "Connection: keep-alive\r\n"
      "\r\n"
      "%s",
      INGEST_PATH, SERVER_HOST, DEVICE_TOKEN, (unsigned)body_len, body);
  if (n <= 0 || (size_t)n >= sizeof(req)) return false;
  return net.print(req) == (size_t)n;
}

// Consume una respuesta HTTP entera (linea de estado, cabeceras, cuerpo).
// Hay que leer el cuerpo completo o la siguiente respuesta de la rafaga se
// desalinea. FastAPI/uvicorn siempre envia Content-Length en estas respuestas;
// si no lo hubiera (chunked), se trata como fallo y se reconecta.
static bool read_http_response(int& status_code) {
  String line = net.readStringUntil('\n');
  if (line.length() < 12) return false;           // ni siquiera "HTTP/1.1 200"
  int sp = line.indexOf(' ');
  if (sp < 0) return false;
  status_code = line.substring(sp + 1, sp + 4).toInt();
  size_t content_length = 0;
  bool has_length = false;
  while (true) {
    line = net.readStringUntil('\n');
    line.trim();                                   // quita el '\r' final
    if (line.length() == 0) break;                 // fin de cabeceras
    int colon = line.indexOf(':');
    if (colon < 0) return false;
    String key = line.substring(0, colon);
    key.toLowerCase();
    if (key == "content-length") {
      content_length = (size_t)line.substring(colon + 1).toInt();
      has_length = true;
    }
  }
  if (!has_length) return false;
  char discard[256];
  size_t remaining = content_length;
  while (remaining > 0) {
    size_t chunk = remaining > sizeof(discard) ? sizeof(discard) : remaining;
    int got = net.readBytes(discard, chunk);
    if (got <= 0) return false;
    remaining -= (size_t)got;
  }
  return true;
}

// Envia hasta max_burst muestras de la cola y confirma. Entrega at-least-once:
// si algo falla a mitad, lo no confirmado se queda en la cola y se reintentara
// (el servidor puede duplicar muestras: no deduplica, ver README seccion 6).
static bool upload_burst(size_t max_burst) {
  if (queue_count == 0) return true;
  if (!ensure_tls_connected()) return false;

  size_t burst = queue_count < max_burst ? queue_count : max_burst;
  size_t written = 0;
  for (size_t i = 0; i < burst; i++) {
    if (!write_post_request(queue_peek(i))) break;
    written++;
  }
  if (written == 0) { net.stop(); return false; }

  size_t consumed = 0;
  for (size_t i = 0; i < written; i++) {
    int code = 0;
    if (!read_http_response(code)) break;          // desalineado: reconectar
    if (code == 200) {
      consumed++;
    } else if (code >= 400 && code < 500) {
      // Muestra rechazada (400/401/413): reintentarla atascaria la cola para
      // siempre. Se descarta y se cuenta, igual que parse_errors del contrato.
      rejected_samples++;
      consumed++;
    } else {
      break;                                        // 5xx: reintentar luego
    }
  }
  queue_pop(consumed);
  if (consumed < written) { net.stop(); return false; }
  return true;
}

// ---------------------------------------------------------------------------
// Muestreo a 50 Hz (contrato seccion 2.1). AQUI SE SUSTITUYE por la lectura
// real del MPU6050/ADXL345 tal como ya la hace src/main.cpp (incluida su
// validacion de rango fisico: una magnitud > 200 m/s^2 no es dato, es basura
// y no debe entrar en la cola).
// ---------------------------------------------------------------------------
static const uint32_t SAMPLE_PERIOD_US = 20000;  // 50 Hz
static uint32_t next_sample_us = 0;

static void poll_sensors() {
  uint32_t now_us = micros();
  if ((int32_t)(now_us - next_sample_us) < 0) return;
  next_sample_us += SAMPLE_PERIOD_US;

  int64_t ts = clock_synced() ? now_ms() : 0;  // 0 => el servidor estampa hora

  // --- MPU6050 (linea DATA del contrato) ---------------------------------
  Sample mpu = {};
  mpu.sensor = SENSOR_MPU6050;
  mpu.ts_ms = ts;
  // SUSTITUIR por mpu.getEvent(...) con la validacion de rango del firmware real.
  mpu.values[0] = 0.121f;   // accel_x [m/s^2]
  mpu.values[1] = -0.043f;  // accel_y
  mpu.values[2] = 9.802f;   // accel_z
  mpu.values[3] = 0.00123f; // gyro_x  [rad/s]  (rad/s SIEMPRE, contrato seccion 3)
  mpu.values[4] = -0.00045f;// gyro_y
  mpu.values[5] = 0.00012f; // gyro_z
  mpu.values[6] = 26.31f;   // temp_c  [C]
  queue_push(mpu);
  // El camino USB sigue vivo: mismo formato que el firmware actual.
  Serial.printf("DATA,%.3f,%.3f,%.3f,%.5f,%.5f,%.5f,%.2f\n",
                mpu.values[0], mpu.values[1], mpu.values[2],
                mpu.values[3], mpu.values[4], mpu.values[5], mpu.values[6]);

  // --- ADXL345 (linea ADXL del contrato) ---------------------------------
  Sample adxl = {};
  adxl.sensor = SENSOR_ADXL345;
  adxl.ts_ms = ts;
  // SUSTITUIR por adxl.getEvent(...) del firmware real.
  adxl.values[0] = 0.020f;
  adxl.values[1] = -0.011f;
  adxl.values[2] = 9.798f;
  queue_push(adxl);
  Serial.printf("ADXL,%.3f,%.3f,%.3f\n",
                adxl.values[0], adxl.values[1], adxl.values[2]);
}

void setup() {
  Serial.begin(115200);  // contrato seccion 2: 115200 8N1
  Serial.println("[UPLINK] Iniciando WiFi/HTTPS (ver dashboard/firmware/README.md)");
  // Cola en HEAP, no en .bss: 2048 x 40 B = 80 KB como estatico no enlaza
  // (desborda dram0_0_seg en 3424 B, ver README seccion 3). Si no hay heap,
  // se avisa en el arranque y queue_push() descarta sin escribir en 0x0.
  queue_buf = (Sample*)malloc(sizeof(Sample) * QUEUE_LEN);
  if (queue_buf == nullptr) {
    Serial.println("[UPLINK] ERROR: sin heap para la cola (hacen falta 80 KB)");
  }
  WiFi.mode(WIFI_STA);
  wifi_next_attempt_ms = 0;   // primer intento inmediato
  next_sample_us = micros();
}

void loop() {
  wifi_maintain();
  time_maintain();
  poll_sensors();  // 50 Hz: mide, imprime por serie y encola. Nunca se bloquea.

  // Subida en rafagas cortas para no bloquear el muestreo: 25 muestras cada
  // 250 ms = 100 muestras/s. ATENCION: upload_burst() es sincrono; con una
  // red mala puede tardar segundos (timeout 5 s por lectura). Ese tiempo es
  // muestreo perdido — aceptable con la cola como colchon. Una version dura
  // moveria la subida a una tarea FreeRTOS; para la fase 1 no hace falta.
  static uint32_t next_upload_ms = 0;
  static uint32_t upload_backoff_ms = 250;
  if (millis() >= next_upload_ms) {
    bool ok = upload_burst(25);
    if (ok) {
      upload_backoff_ms = 250;
    } else {
      // Fallo de red/servidor: backoff exponencial hasta 30 s. La cola
      // acumula mientras tanto (20 s de RAM; para mas, SD — ver README).
      upload_backoff_ms = upload_backoff_ms * 2;
      if (upload_backoff_ms > 30000) upload_backoff_ms = 30000;
    }
    next_upload_ms = millis() + upload_backoff_ms;
  }
}
```

---

## 4.b Variante mínima con `HTTPClient` (una petición por muestra)

El sketch de §4 escribe las peticiones HTTP "a mano" sobre `WiFiClientSecure`
porque necesita la ráfaga con *keep-alive* (varias peticiones escritas antes de
leer sus respuestas). Para quien prefiere la API canónica de Arduino-ESP32,
`HTTPClient` resuelve el **POST JSON** con menos código, a cambio de ser
estrictamente petición-respuesta: cada muestra cuesta un RTT completo.

```cpp
/*
 * Variante mínima del uplink usando HTTPClient (incluido en el core ESP32,
 * sin dependencias). Sirve para la fase 1 (laboratorio, LAN) y para sensores
 * lentos (DS18B20, anemómetro, veleta a 0,1-1 Hz). Para 100 muestras/s sobre
 * 4G NO basta: un POST-respuesta por muestra limita a 1/RTT muestras/s
 * (~12-30 con RTT de 30-80 ms) y las cabeceras pesan más que los datos
 * (ver README seccion 2). Para eso está la ráfaga de la seccion 4.
 */
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>

// POST JSON de una muestra con ts_ms del sensor. Devuelve true solo si el
// servidor respondió 200 ({"ok":true,...}); 4xx = muestra mala, no reintentar.
static bool post_sample_json(const char* json_body) {
  if (WiFi.status() != WL_CONNECTED) return false;   // sin red: encolar (seccion 3)

  WiFiClientSecure client;
  client.setCACert(TLS_ROOT_CA);   // valida el certificado; setInsecure() solo en pruebas
  client.setTimeout(5000);

  HTTPClient https;
  https.setConnectTimeout(5000);
  https.setTimeout(5000);
  if (!https.begin(client, "https://" SERVER_HOST INGEST_PATH)) return false;

  // Cabeceras del contrato (seccion 4.1) + token por dispositivo (seccion 6).
  https.addHeader("Content-Type", "application/json");
  https.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);

  int code = https.POST((uint8_t*)json_body, strlen(json_body));
  // Conviene leer/renovar la conexión en cada POST: HTTPClient puede reutilizar
  // la sesión TLS con setReuse(true), pero en enlaces inestables es preferible
  // abrir y cerrar y dejar la reconexión al backoff del bucle principal.
  https.end();
  return code == 200;
}
```

Serialización del cuerpo (misma función `serialize_sample()` de §4, o este
`snprintf` directo para una muestra puntual):

```cpp
char body[224];
snprintf(body, sizeof(body),
    "{\"sensor\":\"mpu6050\",\"ts_ms\":%lld,"
    "\"accel_x\":%.3f,\"accel_y\":%.3f,\"accel_z\":%.3f,"
    "\"gyro_x\":%.5f,\"gyro_y\":%.5f,\"gyro_z\":%.5f,\"temp_c\":%.2f}",
    (long long)ts_ms, ax, ay, az, gx, gy, gz, temp_c);
bool ok = post_sample_json(body);   // false => meter en la cola y reintentar
```

**Cuándo usar cada uno:**

| | `HTTPClient` (esta sección) | `WiFiClientSecure` a mano (§4) |
|---|---|---|
| Complejidad | mínima | media (gestión de la sesión) |
| Muestras/s sostenibles | ~1/RTT (12-30 en 4G) | ráfagas de 25+ con keep-alive |
| Uso previsto | fase 1 en LAN, sensores lentos, pruebas | el IMU a 50 Hz en campo |

---

## 5. Cómo integrarlo con `src/main.cpp` (fase 1)

El firmware real no se toca hasta que el uplink esté validado. Puntos de
enganche, en orden:

1. **Añadir el bloque de cola + uplink** de §4 como módulo propio
   (`uplink.h`/`uplink.cpp`), sin cambiar el muestreo.
2. **En cada emisión de `DATA,`/`ADXL,`** (donde hoy hay `Serial.printf`),
   construir el `Sample` con los mismos valores ya validados por rango físico y
   llamar `queue_push()`. El `Serial.printf` **se queda**.
3. **Dónde NO enganchar**: en las líneas `[..]` de diagnóstico. El contrato §2.2
   las trata como *eventos*, no como muestras; el endpoint `/api/ingest` hoy
   solo acepta muestras. Mientras no exista un endpoint de eventos, los
   diagnósticos siguen por serie o se ignoran en el camino red.
4. **Presupuesto de tiempo del bucle**: el muestreo a 50 Hz tiene un periodo de
   20 ms y el firmware actual ya lleva validación I2C + diagnósticos dentro de
   ese presupuesto. La subida por red debe ser lo más externa posible al
   camino caliente (por eso las ráfagas cortas de §4).
5. **HX711 / DS18B20 / anemómetro / veleta**: cuando se incorporen, el
   contrato §5.1 prevé una tabla `readings(sensor, ts_ms, name, value, unit)`;
   hasta que exista, **no** se inventan campos en el JSON de `/api/ingest`
   (el servidor los descartaría con 400).

---

## 6. Limitaciones de este diseño (declaradas)

1. **El servidor no valida el token todavía**: la cabecera `Authorization`
   viaja y se ignora. Antes de campo hay que implementar la comprobación
   (`DESPLIEGUE_NUBE.md` §3.2).
2. **Una muestra por petición**: `/api/ingest` acepta un solo objeto JSON. Las
   cabeceras HTTP pesan más que los datos (§2). El cambio mínimo correcto es un
   endpoint de lotes (array JSON o NDJSON); mientras no exista, para un
   despliegue 24/7 sobre 4G hay que aceptar ~45 kB/s de tráfico (~108-117
   GB/mes) o reducir la tasa enviada.
3. **Entrega at-least-once**: un reintento tras un corte a mitad de ráfaga
   puede duplicar muestras en la base de datos (el servidor no deduplica).
   Detección posterior: un `ts_ms` repetido para el mismo sensor en el CSV.
4. **Cola RAM de ~20 s**: para cortes largos hace falta SD (diseño en §3) o
   aceptar perder datos. La NVS/flash interna queda descartada por desgaste.
5. **`upload_burst()` síncrono**: bloquea el muestreo durante los timeouts de
   red. En fase 1 se acepta y se mide; si el efecto es visible en las series
   (huecos regulares), el siguiente paso es una tarea FreeRTOS dedicada.
6. **Sin sincronismo fino entre nodos**: NTP da precisión de decenas de ms,
   suficiente para un nodo; para análisis modal entre nodos de la torre haría
   falta RTC/PTP (Arquitectura §6).
7. **Código no probado en hardware** hasta la fase 1: es código de referencia
   escrito contra la API de Arduino-ESP32 (WiFi, WiFiClientSecure, configTime),
   compilable sin cambios salvo el PEM de la CA y las credenciales. Compilar
   exige que la cola siga **en heap** (`malloc` en `setup()`, §3): con un
   array estático de 2048×40 B el enlace falla por desbordar el DRAM estático.

---

## 7. Cómo probarlo (fase 1 del plan de despliegue)

1. Servidor en el portátil, escuchando en la LAN:

   ```bash
   /Users/ofeliacallirgos/Documents/PlatformIO/Projects/Simulacion Tesis Sensores/dashboard/.venv/bin/python \
     -m server.run --simulate --host 0.0.0.0 --http-port 8000
   ```

   (sin `--simulate` y con el ESP32 por USB: las dos ingestas conviven, USB y
   red, y el dashboard no distingue de qué camino viene cada muestra — ver
   Arquitectura §2.4).

2. Probar el endpoint a mano antes de tocar el ESP32 (respuesta esperada
   `{"ok":true,"derived":{...}}`):

   ```bash
   curl -s -X POST http://<IP-DEL-PORTATIL>:8000/api/ingest \
     -H 'Content-Type: application/json' \
     -d '{"sensor":"mpu6050","ts_ms":1761000000000,
          "accel_x":0.121,"accel_y":-0.043,"accel_z":9.802,
          "gyro_x":0.00123,"gyro_y":-0.00045,"gyro_z":0.00012,"temp_c":26.31}'
   ```

3. Compilar el sketch con `WIFI_*`, `SERVER_HOST` (IP del portátil en la LAN en
   fase 1, con `client.setInsecure()` **temporalmente** porque no hay dominio ni
   certificado) y observar en el monitor serie las líneas `DATA,`/`ADXL,` tal
   como siempre.

4. Verificaciones de salida de la fase 1 (mismas que `DESPLIEGUE_NUBE.md` §7):
   - `/api/status` muestra `n_samples` creciendo a ~100/s por los dos caminos;
   - al apagar el WiFi 30 s, `dropped_samples`/la cola drenan al volver y los
     `ts_ms` siguen siendo continuos (no hay hueco ni amontón);
   - un JSON mal formado (probar a mano) responde 400 y el servidor sigue vivo.
