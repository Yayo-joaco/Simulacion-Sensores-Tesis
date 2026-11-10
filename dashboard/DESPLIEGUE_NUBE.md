# Guía de despliegue en la nube — Monitoreo estructural en Río Santiago

Guía para llevar el dashboard desde el Mac con USB serie hasta un servidor
accesible desde cualquier lugar, con los nodos ESP32 enviando telemetría por
red. Es coherente con [`CONTRATO_DE_DATOS.md`](CONTRATO_DE_DATOS.md) (normativo)
y [`ARQUITECTURA.md`](ARQUITECTURA.md) (diseño). Donde el sistema todavía **no**
está preparado, se dice explícitamente: en una tesis las limitaciones
declaradas valen más que las promesas.

**Estado real del sistema hoy (2026-10), sin adornos:**

| Componente | Estado |
|---|---|
| Backend FastAPI + SQLite + WebSocket | **funciona**, probado con 112k muestras |
| `POST /api/ingest` (contrato §4.1) | **funciona**, acepta `ts_ms` del sensor |
| Dashboard web (sin build, sin CDN) | **funciona** |
| Ingesta USB serie del ESP32 | **funciona** (este es el camino de hoy) |
| Envío WiFi/HTTP desde el firmware | **no implementado** (borrador en [`firmware/README.md`](firmware/README.md)) |
| Autenticación de `/api/ingest` | **no implementada** (ver §3) |
| Cola local / store-and-forward en el ESP32 | **no implementada** (ver §5) |
| Multi-nodo (`node_id`) | **no implementado** (ver §8) |

Precios: todos son **estimados** en órdenes de magnitud (referencia oct-2026,
USD, impuestos no incluidos). Verifícalos con el proveedor antes de comprar.

---

## 1. Opciones de conectividad en el sitio

El nodo genera **100 muestras/s** (MPU6050 a 50 Hz + ADXL345 a 50 Hz). El
presupuesto de datos por segundo condiciona toda elección de enlace, así que va
primero.

### 1.0 Presupuesto de datos del nodo (base de todo lo demás)

| Concepto | Cálculo | Valor |
|---|---|---|
| Muestras/s | 2 sensores × 50 Hz | 100 |
| JSON por muestra MPU6050 (contrato §4.1) | `{"sensor":..., "ts_ms":..., 7 valores}` | ~158 B |
| JSON por muestra ADXL345 | 3 valores | ~91 B |
| **Payload útil/s** | 50×158 + 50×91 | **~12,5 kB/s (~100 kbit/s)** |
| Cabeceras HTTP req+resp por POST (una muestra por POST) | ~327 B × 100 req/s | ~33 kB/s |
| **Tasa real por el aire con el firmware actual, 1 POST/muestra** | 12,5 + 33 | **~45 kB/s (~360 kbit/s)** |
| Volumen mensual, 1 POST/muestra, 24/7 | 45 kB/s × 86400 × 30 | **~117 GB/mes** (108-117 según el peso exacto de las cabeceras) |
| **ESCENARIO FUTURO**: tasa con envío por lotes (50 muestras/POST, 2 POST/s) | 12,5 + 2×0,3 | **~13 kB/s (~100 kbit/s)** |
| **ESCENARIO FUTURO**: volumen mensual en lotes, 24/7 | 12,5 kB/s × 86400 × 30 | **~32 GB/mes** |

La fila de lotes es un **escenario futuro, no el ritmo operativo**: exige un
endpoint de arrays que hoy no existe (pendiente en §8.5) y el firmware de
referencia hace 1 POST por muestra aunque use keep-alive. Las ráfagas con
keep-alive resuelven el límite del RTT (§1.2), **no** el peso de las cabeceras:
las ~33 kB/s de cabeceras se pagan igualmente, 100 veces por segundo.

Conclusión operativa: **el JSON de las muestras es barato; las cabeceras HTTP
por muestra son la ruina**. Con el firmware actual (1 POST/muestra), un
despliegue 24/7 sobre 4G necesita un plan de **~108-117 GB/mes**. Los ~32-50
GB/mes solo son alcanzables con el endpoint de lotes (pendiente, §8.5) o
reduciendo la tasa enviada (ver §5 y `firmware/README.md`).

### 1.1 a. WiFi existente de la estación base — preferente

Si la torre de Río Santiago (o la estación base) tiene un punto de acceso WiFi
con salida a Internet, es la opción primera: cero coste añadido, latencia baja
(<20 ms), ancho de banda sobrado.

* **Cuándo**: siempre que haya cobertura WiFi donde se instala el nodo (torre,
  edificio de la estación base).
* **Requisitos**: SSID/contraseña WPA2-AES o mejor; que el operador permita
  conectar el ESP32 (algunas redes corporativas filtran por MAC o exigen
  802.1x, que el ESP32 soporta mal).
* **Riesgo real**: la red de la estación puede ser una red de gestión con
  firewall hacia Internet. Probar `ping`/`curl` hacia el VPS antes de instalar.
* **Coste**: 0 S/ por el enlace (el WiFi ya existe y ya se paga).

### 1.2 b. Router 4G/LTE con SIM de datos — sin WiFi en el sitio

Es el escenario más probable en un tramo de red troncal rural.

* **Hardware**: router 4G industrial con Ethernet/WiFi y watchdog de
  reconexión (Teltonika RUT2xx/RUT9xx, ~US$ 120-180; alternativas genéricas
  Huawei/TP-Link ~US$ 60-120). Un módulo 4G directo sobre el ESP32
  (SIM800/A7670, ~US$ 25-40) es más barato pero peor: antenna exigente, TLS
  lento, AT-commands compiten con el muestreo a 50 Hz. **Recomendación: router
  4G aparte.** El ESP32 habla WiFi al router y se desentiende del enlace.
* **Datos**: el presupuesto de §1.0 da **~108-117 GB/mes** con el firmware
  actual (1 POST por muestra, tasa completa). Un plan de 20-50 GB (US$ 15-40/mes
  en operadores latinoamericanos, estimado; verificar en Claro/Movistar/Entel
  con un plan M2M, que además trae IP privada y mejor cobertura en zona rural
  que un plan de consumo) **no cubre ese ritmo**: solo basta con el endpoint de
  lotes (pendiente, §8.5, ~32 GB/mes) o reduciendo la tasa enviada. Para 24/7 a
  tasa completa hay que contratar en consecuencia (~100 GB/mes o más).
* **Latencia típica**: 30-80 ms de RTT. Esto limita el envío
  petición-respuesta-sincrónica a ~1/RTT = 12-30 muestras/s; por eso el ejemplo
  de firmware manda ráfagas con keep-alive (ver `firmware/README.md`). Las
  ráfagas resuelven **solo** el límite del RTT: no ahorran las cabeceras HTTP
  (§1.0), que se pagan en cada POST.
* **Antena**: en torre, antena externa direccional. La diferencia entre
  antena interior y exterior suele ser el fallo del sistema entero.

### 1.3 c. LoRaWAN + pasarela — solo si la distancia lo justifica (y NO sirve para el IMU crudo)

Este apartado es una conclusión útil para la tesis, con cuentas que se pueden
incluir en el capítulo de diseño de la WSN.

**Ancho de banda real de LoRa.** Dos conceptos que no deben confundirse: el
data rate *instantáneo* de la modulación (lo que cabe en el aire en un
instante) y el caudal *medio útil* que queda después del duty cycle (lo que un
nodo puede transmitir de forma sostenida):

| Parámetro | Valor |
|---|---|
| (a) Data rate instantáneo, SF7/125 kHz (la más rápida) | ~5,5 kbit/s brutos |
| (a) Data rate instantáneo, SF12/125 kHz (larga distancia) | 0,3 kbit/s brutos (DR0 de LoRaWAN; rango LoRa Alliance SF12→SF7: 0,3–5,5 kbit/s) |
| Trama de 51 B a SF7/125 | ~110 ms de aire → 1 trama cada ~11 s con 1% de duty cycle |
| Trama de 51 B a SF12/125 (larga distancia; *low-data-rate optimize* obligatorio a SF12) | **~2,1 s sin LDR / ~2,5 s con LDR** de aire → 1 trama cada **~3,5–4,2 min** con 1% de duty cycle |
| (b) Caudal medio útil por nodo con duty cycle del 1% (868 MHz; fair-use similar en 915 MHz) | **~30–60 bit/s** (51 B = 408 bits cada ~11 s ≈ 37 bit/s a SF7) |

Tiempos de aire calculados con la fórmula del driver Semtech —mismo resultado
en la calculadora de referencia
<https://avbentem.github.io/airtime-calculator/> (fuente a citar en el
capítulo de la WSN)—: una trama de 51 B son 408 bits y a SF12/125 el máximo
son 293 bit/s (CR 4/5), así que el suelo informacional es ~1,4 s de payload +
0,4 s de preámbulo + 0,26 s de cabecera: los ~1,5 s totales que se citaban
antes eran físicamente imposibles. La fila derivada correcta es «1 trama cada
~3,5–4,2 min al 1% de duty cycle», no «cada ~2,5 min».

**Lo que pide el nodo:**

| Necesidad | Cálculo | Valor |
|---|---|---|
| MPU6050 a 50 Hz comprimido al mínimo (ts + 6 ejes int16) | 50 × 24 B | 1.2 kB/s = **9,6 kbit/s** |
| Añadiendo ADXL345 a 50 Hz | 100 × 24 B | 2.4 kB/s = **19,2 kbit/s** |
| Déficit frente al data rate instantáneo (0,3–1 kbit/s, SF12–SF10) | 19,2 / 1 → 19,2 / 0,3 | **×19 a ×64 insuficiente** |
| Déficit frente al caudal medio útil con 1% de duty (30–60 bit/s/nodo) | 19 200 / 60 → 19 200 / 30 | **×320 a ×640 insuficiente** (~×300-600) |
| Déficit incluso frente al teórico instantáneo (5,5 kbit/s a SF7) | 19,2 / 5,5 | **×3,5 insuficiente** |

**Conclusión**: el MPU6050 a 50 Hz **no cabe en LoRa** ni con la mejor
modulación: se necesitan al menos ~20 kbit/s sostenidos y un enlace LoRaWAN
real da, como mucho, kbit/s instantáneos compartidos y —con el duty cycle del
1%— un caudal medio útil de decenas de bit/s por nodo, con tramas de segundos.
LoRa sí sirve, y bien, para:

* resúmenes de 1 Hz (`vib_rms`, `temp_c`, `roll/pitch`) — ~200 B/s, justo en
  el orden de magnitud de LoRa;
* alarmas de umbral de vibración (eventos puntuales);
* nodos de sensores lentos del capítulo futuro (DS18B20, anemómetro, veleta a
  0,1–1 Hz).

Para el análisis modal de la torre (que es lo que necesita el IMU crudo a
50 Hz): WiFi, 4G o Ethernet. Esto se documenta como **restricción de la
tecnología**, no como fallo del diseño: la WSN heterogénea (LoRa para lo
lento, 4G para el IMU) es una arquitectura legítima y defendible en la tesis.

* **Coste si aun así se monta**: pasarela 8 canales (RAK7249/SX1302)
  US$ 150-350 + nodo LoRa US$ 15-25 + servidor de red (TTN self-hosted o The
  Things Network). No compre esto para el IMU.

### 1.4 d. Enlace satelital / Starlink — último recurso

Para Río Santiago sin cobertura celular útil. Starlink (kit ~US$ 350-500 +
suscripción ~US$ 40-60/mes, estimado oct-2026) entrega ancho de banda de sobra
(>50 Mbit/s) y latencia de 25-60 ms. Problemas: consumo eléctrico del terminal
(25-75 W, frente a <5 W del router 4G), visibilidad del cielo, y que en la
instalación de una torre rural la energía es el recurso caro.

* **Cuándo**: cuando no haya 3G/4G usable y el WiFi de la estación no llegue.
* **Alternativa satelital IoT** (Swarm —servicio discontinuado—, Iridium
  SBD, Orbcomm): mensajes de decenas de bytes, mismo problema que LoRa: no
  sirve para 50 Hz, solo para resúmenes.
* **Orden recomendado**: WiFi estación → 4G → Starlink → (resúmenes por
  satélite/LoRa) → nada.

---

## 2. Dónde corre el servidor

Tres arquitecturas. Todas ejecutan el mismo `server/` sin cambios (el frontend
tampoco cambia).

### 2.1 VPS económico (recomendado para fase 2-3)

Dimensionado para **100 muestras/s** (2 sensores × 50 Hz) con SQLite y
WebSocket:

| Recurso | Necesario | Por qué |
|---|---|---|
| CPU | 1 vCPU suficiente (2 cómodos) | el trabajo es E/S: FastAPI + escrituras por lotes (config.py: 50 filas / 200 ms) + broadcast WS. Uso esperado <20% de un núcleo |
| RAM | **1-2 GB** | proceso Python ~250-400 MB + page cache de SQLite |
| Disco | **50-80 GB** | ARQUITECTURA §5.3: ~1,5-2,5 GB/día en SQLite → 3-4 semanas de campaña continua |
| Red | 100-200 Mbit/s de sobra | entrada ~45 kB/s con el firmware actual, 1 POST/muestra (~13 kB/s solo con lotes futuros, §1.0); salida: WS a pocos clientes + exportaciones CSV puntuales |
| Backups | 20-50 GB externos | copia diaria del `.db` (apéndice C) |

Precios estimados (comparar en el momento de contratar):

| Proveedor | Instancia típica | Coste estimado/mes | Notas |
|---|---|---|---|
| Hetzner Cloud | CX22/CX32 (2 vCPU, 2-4 GB, 40-80 GB) | **~US$ 5-10** | el más barato de calidad; DC en EU/US, latencia aceptable desde Perú |
| DigitalOcean | Basic 1-2 GB / 25-50 GB | **~US$ 6-12** | + volumen ~US$ 0.10/GB/mes si se necesita más disco |
| AWS Lightsail | 1-2 GB / 40-60 GB | **~US$ 5-10** | cómodo si ya se usa AWS; tráfico de salida incluido generoso |
| Oracle Cloud Free Tier | VM Ampere A1 (4 OCPU/24 GB) + 200 GB | **US$ 0** | *always free* real, pero la disponibilidad de capacidad es lotería y la alta de cuenta es lenta. Si sale, es la mejor relación prestaciones/precio |

Estimación de trabajo mensual del servidor (para justificar el dimensionado):

| Magnitud | Valor |
|---|---|
| Filas/día | 8,64 × 10⁶ (100/s × 86400) |
| Crecimiento de `monitoreo.db` | ~1,5-2,5 GB/día |
| Escrituras SQLite/s (por lotes) | 2-10 transacciones/s |
| Mensajes WebSocket/s | 100 × nº de clientes conectados |

### 2.2 Raspberry Pi 4/5 en la propia estación base + Tailscale

El servidor corre **en el sitio** y el acceso remoto va por VPN (Tailscale),
no por Internet público.

| Concepto | Detalle |
|---|---|
| Hardware | Pi 4 (4 GB) ~US$ 60-80 o Pi 5 ~US$ 90-120 + SSD/SD 64-128 GB US$ 15-30 + PSU US$ 10 + caja US$ 10 ≈ **US$ 120-200** |
| Software | el mismo `server/`; Tailscale (plan gratuito para uso personal) da IP estable, TLS y control de acceso |
| Autonomía | con una UPS/POE pequeña (~US$ 30-60) sigue midiendo aunque caiga el enlace externo: **la medición no depende de Internet** |
| Acceso | `tailscale serve` publica el dashboard con HTTPS y autenticación de cuenta Tailscale sin tocar el backend |
| Coste mensual | **~0** (solo el kWh) |

**Comparación honesta:**

| Criterio | VPS | Raspberry Pi local |
|---|---|---|
| Acceso desde cualquier lugar | directo (HTTPS) | vía Tailscale (funciona bien, requiere cuenta/red) |
| Medición si se corta Internet | se corta la ingesta (salvo cola del ESP32) | **sigue midiendo y almacenando** |
| Backups fuera del sitio | fáciles (snapshot del proveedor) | hay que copiar a otro lado (rsync/Tailscale a un PC) |
| Mantenimiento | actualizaciones del proveedor, snapshot | SD/SSD, fuente, temperatura en caja |
| Riesgo de pérdida total | bajo | robo/dano del equipo en la torre = pérdida del histórico |
| Coste | 6-12 US$/mes | 120-200 US$ una vez |

Recomendación para la tesis: **VPS para el dashboard permanente**, y si se
dispone de hardware, una Pi local como respaldo de autonomía en campañas. No
son excluyentes: la Pi puede replicar su `.db` al VPS.

### 2.3 Nube gestionada (AWS IoT Core / Azure IoT Hub / Google Cloud IoT)

**Cuándo compensa**: cientos-miles de dispositivos, provisioning masivo de
credenciales, reglas de ingesta a otros servicios, SLA y multi-región.

**Por qué es overkill aquí**, con números (estimados):

| Plataforma | Precio típico (estimado) | Aplicado a este sistema |
|---|---|---|
| AWS IoT Core | ~US$ 1 por millón de mensajes + conectividad por minuto | 8,64 M muestras/día/nodo = 260 M mensajes/mes → **~US$ 200-300/mes/nodo** a una muestra por mensaje; con lotes de 1 msg/s → ~US$ 3-10/mes |
| Azure IoT Hub | Basic B1 ~US$ 10/mes (400k msgs/día) | **insuficiente** para 8,6 M msgs/día sin lotes; S1 ~US$ 25-50/mes |
| Google Cloud IoT | (producto retirado del catálogo) | n/a |

Y además: cada plataforma obliga a un puente MQTT→`/api/ingest` o a reescribir
el repositorio, suma una consola, unos credenciales y un ciclo de facturación
que hay que explicar en la tesis. **Con 1-5 nodos, un VPS de 6-12 US$/mes con
FastAPI es más barato, reproducible y suficiente.** La capa `Repository`
(Arquitectura §2.2) deja la puerta abierta si algún día cambia el contexto.

---

## 3. Seguridad (esto es serio)

### 3.1 El problema actual: `/api/ingest` no tiene autenticación

Verificado en `server/app.py`: el endpoint acepta cualquier JSON válido sin
credenciales. Riesgos concretos en campo:

1. **Inyección de muestras falsas**: cualquiera que encuentre la URL puede
   escribir en `monitoreo.db`. Para una tesis, eso es contaminar la evidencia.
2. **Denegación de servicio**: un generador de tráfico llena el disco
   (1,5-2,5 GB/día por nodo; un atacante puede hacerlo mucho más rápido).
3. **Volcado de datos**: `GET /api/export.csv` descarga **toda** la base de
   datos sin credenciales.

### 3.2 Mecanismo mínimo recomendado: token por dispositivo

```
POST /api/ingest HTTP/1.1
Host: monitoreo.ejemplo.org
Authorization: Bearer <token-de-este-nodo>
Content-Type: application/json

{"sensor": "mpu6050", "ts_ms": ..., ...}
```

* Un token aleatorio de 32 bytes por nodo (`secrets.token_urlsafe(32)` /
  `os.urandom`). Un token por dispositivo, no uno compartido: si se compromete
  uno, se revoca solo ese.
* El servidor compara con `secrets.compare_digest` (tiempo constante) y
  responde **401** si falta o no coincide. Mínimo: ~15 líneas en `app.py`.
* Almacenamiento: en el ESP32, en **NVS** (`Preferences`), **nunca** hardcode
  en el fuente que se publica; en el servidor, fichero con permisos `600` o
  variable de entorno.
* Revocación = cambiar el token del nodo en la lista del servidor.
* **Riesgos que el token no resuelve**: si el enlace va sin TLS, el token viaja
  en claro (por eso TLS es obligatorio, no opcional); si alguien copia la flash
  del ESP32, obtiene el token (mitigación: cifrar la NVS, aceptar el riesgo
  como residual en una tesis).

**Estado: NO implementado.** El firmware de ejemplo ya manda la cabecera
`Authorization: Bearer ...` para no tener que tocar el firmware después; el
servidor la ignora hoy. Implementarlo es el paso previo indispensable a
cualquier despliegue fuera del laboratorio.

### 3.3 TLS obligatorio

* **Let's Encrypt con Caddy** (recomendado): Caddy obtiene y renueva el
  certificado solo. El VPS debe tener el dominio apuntando a su IP y el puerto
  80 accesible desde Internet (HTTP-01). Sin dominio, no hay Let's Encrypt:
  opción válida es Tailscale (`tailscale serve` ya da HTTPS con certificado).
* **El ESP32 debe validar el certificado**: embebido el PEM de la CA raíz
  (ISRG Root X1 para Let's Encrypt) con `WiFiClientSecure::setCACert()`.
  `setInsecure()` es solo para el banco de pruebas y está mal usarlo en campo:
  sin validación, un atacante se pone en medio y roba el token.
* El dashboard pasa de `http://` a `https://` y `/ws` a `wss://`. El frontend
  **no cambia**: la URL del WebSocket se deriva de `location`, ya es relativa.

### 3.4 El dashboard detrás de autenticación

El dashboard y sobre todo `/api/export.csv` no deben estar en abierto:

* **Opción mínima (fase 2)**: `basic_auth` de Caddy con hash bcrypt (apéndice B).
  Vale para 1-3 usuarios (tesista, asesor).
* **Opción cómoda (fase 1-3)**: **Tailscale**. El VPS entra en la tailnet, el
  dashboard se sirve solo dentro de la VPN y además se gana el acceso SSH sin
  exponer el puerto 22.
* El backend sigue escuchando en `127.0.0.1` (config.py `HTTP_HOST`) y **no se
  expone directamente a Internet**: siempre detrás del proxy. En el VPS:
  `python -m server.run --host 127.0.0.1` (o el `systemd` del apéndice A).

### 3.5 WiFi, router 4G y VPS

* WiFi: WPA2-AES como mínimo, contraseña larga; cambiar SSID si es el de
  fábrica; **desactivar WPS**.
* Router 4G: cambiar `admin/admin` de fábrica (¡mayoría de routers rurales lo
  traen!), desactivar administración remota/UPnP, PIN de SIM, APN del plan
  M2M. Si el router soporta WireGuard/Tailscale, tunelizar el tráfico del nodo
  al VPS y cerrar todo lo demás.
* VPS: firewall solo 22/80/443 (`ufw`), SSH por clave sin contraseña,
  `fail2ban`, actualizaciones automáticas de seguridad, backups cifrados fuera
  del VPS (apéndice C).

---

## 4. Arquitectura objetivo

```
   ┌─────────────────────────────┐
   │  Nodo sensor (Río Santiago) │
   │  ESP32 + MPU6050 + ADXL345  │
   │  (+ HX711, DS18B20, viento) │
   │  · muestreo 50 Hz           │
   │  · cola local sin red (§5)  │
   └──────────────┬──────────────┘
                  │ WiFi (al router) o WiFi→router 4G
                  │ HTTPS POST /api/ingest  (JSON, Bearer token; 1 POST/muestra)
                  │ TLS (Let's Encrypt)  ~45 kB/s (1 POST/muestra; lotes futuros ~13 kB/s)
   ┌──────────────▼──────────────────────────────────────────────┐
   │  VPS (Hetzner/DO/Lightsail/Oracle)  — o Raspberry Pi local  │
   │  ┌─────────────────────────────────────────────────────┐    │
   │  │ Caddy :443  (TLS + basic_auth, solo proxy)          │    │
   │  │   └─► 127.0.0.1:8000  uvicorn + FastAPI             │    │
   │  │         · POST /api/ingest  → derived.py → SQLite   │    │
   │  │         · /ws  broadcast · /api/history · export    │    │
   │  │       data/monitoreo.db  (backup diario)            │    │
   │  └─────────────────────────────────────────────────────┘    │
   └──────────────┬──────────────────────────────────────────────┘
                  │ HTTPS + WSS (WebSocket)
   ┌──────────────▼──────────────┐
   │  Navegador, en cualquier    │
   │  lugar: dashboard en vivo   │
   │  + histórico + export CSV   │
   └─────────────────────────────┘
```

### Qué ficheros cambian y cuáles no

| Fichero | ¿Cambia para el despliegue? | Detalle |
|---|---|---|
| `server/*` | **No** (para la fase 2-3) | `POST /api/ingest` ya existe y ya acepta `ts_ms` del sensor (contrato §4.1) |
| `web/*` | **No** | el frontend es estático y sus URLs son relativas; el proxy le es transparente |
| `CONTRATO_DE_DATOS.md` | **No** | el contrato ya contempla la ingesta por red |
| `server/config.py` | Solo si se quiere cambiar el puerto por defecto | los flags de `run.py` (`--host`, `--http-port`, `--db`) bastan |
| firmware (nuevo) | **Sí** | WiFi + NTP + HTTPS + cola local: ver [`firmware/README.md`](firmware/README.md) |
| `server/app.py` (futuro) | **Sí, mínimo** | autenticación Bearer (~15 líneas) y opcionalmente ingesta por lotes |
| `server/db.py` (futuro) | Opcional | columnas `node_id` y `received_ms` para multi-nodo (§8) |

Es deliberado que el despliegue no toque el backend: la ingesta por red es un
**detalle de despliegue**, no una propiedad de la telemetría (Arquitectura §2.4).

---

## 5. Tolerancia a fallos de red

La red rural cae. El sistema debe seguir midiendo y no perder la línea temporal.

### 5.1 Cola local en el ESP32 (diseño; no implementado aún)

Cálculo del tamaño de cola (valores verificables en §1.0 y `firmware/README.md`):

| Concepto | Cálculo | Valor |
|---|---|---|
| Muestra empaquetada en RAM (`ts_ms` int64 + tipo/pad 4 B + 7 floats) | struct de 40 B (verificado con `sizeof`) | **40 B** |
| Ritmo de captura | 100 muestras/s × 40 B | 4,0 kB/s |
| Cola RAM típica (2048 muestras) | 2048 × 40 B | **~80 KB** → **~20 s** de red caída (2048 / 100 s⁻¹) |
| Tras un minuto sin red en RAM | — | inaceptable: el heap del ESP32 con WiFi+TLS libre es ~150-250 KB |

Opciones por duración del corte:

| Duración del corte | Dónde guardar | Cálculo | Coste |
|---|---|---|---|
| <20 s | RAM (cola circular) | 80 KB | 0 |
| minutos-horas | **tarjeta SD** (append-only) | 4,0 kB/s = 14 MB/h = **346 MB/día** → una SD de 8 GB = **~23 días** | ~US$ 5 (módulo + SD) |
| días | SD **con degradación**: sin red, muestrear a 5 Hz y guardar 10 muestras/s | 400 B/s = 35 MB/día → 8 GB = **~7 meses** | idem |
| NVS / flash interna | **descartado por diseño** | 4,0 kB/s = 346 MB/día acabaría con la flash (desgaste) y NVS es para configuración | — |

Reglas de diseño de la cola (las que implementa el ejemplo de firmware):

1. Orden FIFO estricto; si se llena, se descarta la muestra **nueva** y se
   cuenta el descarte (conserva el arranque del corte, que es lo interesante
   para analizar la brecha).
2. El reenvío es **en ráfagas** por una conexión HTTPS con keep-alive: se
   escriben N peticiones y se leen N respuestas. Con una petición-respuesta
   sincrónica sobre 4G (RTT ~40 ms) el techo es ~25 muestras/s: insuficiente
   para 100 muestras/s.
3. Lo reenviado se marca como tal: la muestra conserva su `ts_ms` original, no
   la hora de reenvío.

### 5.2 El lado del servidor: `ts_ms` del sensor

* **Ya funciona**: `POST /api/ingest` acepta `ts_ms` opcional y lo persiste
  (contrato §4.1; verificado en `server/app.py` → `IngestBody`). Con cola y
  reenvío, **la línea temporal se conserva**: las muestras de un corte de red
  aparecen en su hueco real, no amontonadas al recuperar la conexión.
* Si el nodo envía `ts_ms` ausente o 0, el servidor usa la hora de recepción
  (útil antes de sincronizar el reloj con NTP).
* **No implementado hoy**: `received_ms` (hora de llegada) como columna propia.
  Con reenvío por lotes, tener ambas marcas permite medir el desfase y detectar
  cortes; es un cambio pequeño en `db.py`/`app.py` y está previsto (Arquitectura §5.2).
* **Matiz honesto sobre `connected`**: el watchdog de 2 s del contrato §2.3
  mide **llegada de datos**, no estado del sensor. Con reenvío por lotes, si el
  nodo está 10 min sin red y luego vuelca la cola, `connected` parpadeará como
  «caído» durante el corte aunque el sensor midiera perfectamente. En un
  despliegue real habría que separar «sensor midiendo» de «enlace activo».
* **Reloj**: sin NTP no hay `ts_ms` fiable. El nodo debe sincronizarse
  (`configTime` + NTP, ver `firmware/README.md`) antes de emitir `ts_ms` propio,
  y marcar como «sin sincronizar» los periodos anteriores. Para análisis modal
  entre nodos (fase 4) se necesita sincronismo real (NTP/PTP/RTC con
  compensación), tal como ya advierte Arquitectura §6.

---

## 6. Coste total estimado

Estimaciones en USD, órdenes de magnitud, impuestos no incluidos. La columna
«realista» es la que se recomienda para la tesis.

### 6.1 BOM de un nodo (hardware)

| Parte | Laboratorio (fase 1) | Campo en torre (fase 3) |
|---|---|---|
| ESP32 DevKit | 8-15 | 8-15 |
| MPU6050 (GY-521) | 3-6 | 3-6 |
| ADXL345 | 5-12 | 5-12 |
| Alimentación | USB 5 V (5) | PSU industrial 24 V/5 V o solar 30 W + batería 12 Ah + regulador (80-180) |
| Estanqueidad | caja simple (5) | caja IP65 + prensaestopas (15-30) |
| Cables, montaje, puesta a tierra | 5-10 | 15-30 |
| Router 4G + antena externa | — | 60-180 |
| Módulo SD (cola local) | — | 3-5 |
| **Total nodo** | **~US$ 30-60** | **~US$ 250-500** |

Futuros sensores (no en este presupuesto): HX711 + celda 10-25, DS18B20 2-4,
anemómetro + veleta 30-80.

### 6.2 Operación mensual/anual

| Concepto | 1 nodo | 5 nodos |
|---|---|---|
| VPS (2 GB / 50-80 GB) | 6-12 US$/mes | 12-30 US$/mes (más disco: +0.10 US$/GB/mes) |
| SIM M2M 20-50 GB (solo si no hay WiFi) | 15-40 US$/mes por nodo | 75-200 US$/mes |
| Dominio | ~10 US$/año | idem (uno solo) |
| Backups externos | 0-5 US$/mes | 5-15 US$/mes |
| **Subtotal/año (con WiFi en sitio)** | **~US$ 80-220/año** | **~US$ 160-400/año** |
| **Subtotal/año (con 4G por nodo)** | **~US$ 260-700/año** | **~US$ 1,100-2,900/año** |

### 6.3 Total primer año (hardware + operación)

| Escenario | 1 nodo | 5 nodos |
|---|---|---|
| Laboratorio PUCP (WiFi propio) | **~US$ 110-280** | ~US$ 220-600 |
| Campo Río Santiago con 4G | **~US$ 500-1,200** | **~US$ 2,400-5,400** |
| Campo con Starlink (si no hay 4G) | +450-700 (kit) + 480-720/año | +1 kit compartido |

Observación para 5 nodos: el VPS es compartido (el coste no se multiplica) pero
el **volumen** sí: 5 nodos × 2 GB/día = **7-12 GB/día** de SQLite
(~220-370 GB/mes). Eso obliga a la retención/agregados horarios que prevé
Arquitectura §5.3, o a discos de 250-500 GB. Es la razón técnica, no la
económica, la que marca el límite de SQLite con varios nodos.

---

## 7. Plan de despliegue por fases

### Fase 0 — Hoy: USB serie en el Mac (hecho)

* Estado: funciona. `./.venv/bin/python -m server.run` con el ESP32 en
  `/dev/cu.usbserial-0001`, o `--simulate` sin hardware.
* Objetivo de la fase: validación de electrónica y firmware sin depender de la
  red (Arquitectura §2.4). **No se retira**: sigue siendo el banco de pruebas
  del protocolo serie.

### Fase 1 — WiFi en el laboratorio PUCP (próxima)

* Firmware: añadir WiFi + NTP + `POST /api/ingest` (guía: `firmware/README.md`).
* Servidor: el mismo, en el portátil, con `--host 0.0.0.0` **solo en la red del
  laboratorio**.
* Verificaciones de salida:
  1. Las muestras por WiFi aparecen en el dashboard igual que por USB;
  2. latencia medible (<100 ms de extremo a extremo en LAN);
  3. se cumple el ritmo de 100 muestras/s en LAN (medir con `n_samples` de
     `/api/status`);
  4. al quitar el WiFi 30 s y restaurarlo, la cola se vacía sin perder muestras
     (comprobar por `ts_ms` continuos).
* Prueba de humo sin hardware: `curl -X POST ... /api/ingest` (apéndice D).

### Fase 2 — VPS + WiFi local (acceso desde cualquier lugar)

1. Contratar VPS (§2.1), dominio opcional (o Tailscale).
2. Desplegar `server/` con el apéndice A (systemd) + apéndice B (Caddy/TLS).
3. **Antes de abrirlo a Internet: implementar el token Bearer (§3.2).**
4. Firewall: solo 80/443 (y 22 si no se usa Tailscale).
5. Backup diario del `.db` (apéndice C).
6. Verificaciones: dashboard accesible por HTTPS desde una red externa (datos
   móviles del teléfono); WebSocket `wss://` vivo; export CSV protegido;
   `curl` de ingesta con token incorrecto → 401.
7. Truco útil antes de tener hardware: `python -m server.run --simulate` en el
  VPS para demostrar el dashboard remoto con datos sintéticos.

### Fase 3 — 4G en Río Santiago

* Instalar router 4G + antena externa, caja IP65, alimentación (solar/UPS),
  puesta a tierra.
* SIM M2M con APN propio; verificar cobertura con un test de campo antes de
  montar en la torre (medir RSRP/RSRQ con el propio router).
* Firmware con cola local + ráfagas (§5); NTP obligatorio.
* Vigilancia: `/api/health` sondeado (cron + healthchecks.io o similar), log de
  cortes de red derivado de `ts_ms` vs `received_ms` (cuando exista).
* Retención: política de purga/agregados antes de que `monitoreo.db` llene el
  disco (2,5 GB/día/nodo).

### Fase 4 — Varios nodos

* Contrato: añadir `node_id` a `samples`, `events` y al mensaje `sample`
  (Arquitectura §5.2); el dashboard pasa a tener selector de nodo.
* Un token por nodo; revocación independiente.
* Sincronismo de reloj real entre nodos (NTP con corrección, o RTC) — sin esto
  el análisis modal entre nodos no es válido.
* Persistencia: PostgreSQL/TimescaleDB o retención + agregados horarios en
  SQLite; un solo proceso de ingesta (SQLite tiene un escritor).
* Un nodo caído = un `sensor_status` con `present=0`, nunca un servidor caído.

---

## 8. Limitaciones declaradas (para el capítulo de la tesis)

1. **Sin autenticación ni TLS en el backend** (Arquitectura §6): el token
   Bearer está diseñado (§3.2) pero **no implementado**; el TLS se aporta desde
   fuera (Caddy/Tailscale), que es además la separación recomendada.
2. **Sin cola local en el firmware**: el ejemplo de `firmware/README.md` es un
   diseño con código de referencia, no firmware probado de campo.
3. **Sin multi-nodo**: `samples` no distingue emisores; hoy solo hay un nodo.
4. **Sin `received_ms`**: no se puede medir el desfase entre medición y
   llegada; con reenvío por lotes esa métrica sería necesaria.
5. **Una muestra por petición en `/api/ingest`**: con 100 muestras/s las
   cabeceras HTTP pesan más que los datos (§1.0). Un endpoint de lotes
   (JSON array) es el cambio mínimo que desbloquea el 24/7 sobre 4G.
6. **Sincronización por llegada**: con un solo nodo es suficiente; para varios
   nodos no lo es.
7. **Precios estimados**: verificar con proveedores antes de comprometer
   presupuesto; el consumo de datos real se mide en la fase 1 y corrige el
   plan de datos.

---

## Apéndice A — Servicio systemd en el VPS

`/etc/systemd/system/monitoreo.service`:

```ini
# Servidor de ingesta del dashboard de monitoreo estructural.
# Escucha SOLO en 127.0.0.1: Caddy es quien habla con Internet (§3.4).
[Unit]
Description=Monitoreo estructural - servidor de ingesta (FastAPI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=monitoreo
WorkingDirectory=/opt/monitoreo/dashboard
ExecStart=/opt/monitoreo/dashboard/.venv/bin/python -m server.run --host 127.0.0.1 --http-port 8000
Restart=always
RestartSec=5
# Sin buffer de Python para que los logs lleguen a journald al momento.
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Instalación mínima verificada paso a paso:

```bash
sudo useradd --system --home /opt/monitoreo --shell /usr/sbin/nologin monitoreo
sudo mkdir -p /opt/monitoreo && sudo chown monitoreo /opt/monitoreo
# copiar el proyecto (scp/git) a /opt/monitoreo/dashboard
sudo -u monitoreo python3 -m venv /opt/monitoreo/dashboard/.venv
sudo -u monitoreo /opt/monitoreo/dashboard/.venv/bin/pip install -r /opt/monitoreo/dashboard/requirements.txt
sudo systemctl enable --now monitoreo
systemctl status monitoreo           # debe estar active (running)
curl http://127.0.0.1:8000/api/health   # {"status":"ok",...}
```

## Apéndice B — Caddy (TLS + autenticación básica)

`/etc/caddy/Caddyfile` (dominio de ejemplo):

```caddyfile
# Caddy obtiene y renueva el certificado Let's Encrypt solo.
# basic_auth protege dashboard Y exportaciones; el token del nodo va en el
# header de ingesta (§3.2), no en la contraseña del navegador.
monitoreo.ejemplo.org {
    basic_auth {
        # generar hash: caddy hash-password
        tesista $2a$14$REEMPLAZAR_CON_HASH_BCRYPT
    }
    reverse_proxy 127.0.0.1:8000
}
```

Notas: `reverse_proxy` de Caddy soporta el WebSocket `/ws` sin configuración
extra; sin dominio público, usar `tailscale serve` y saltarse este apéndice.

## Apéndice C — Copias de seguridad de `monitoreo.db`

SQLite no se copia bien con `cp` mientras se escribe; usar el API de backup:

```bash
#!/bin/sh
# /opt/monitoreo/backup.sh - copia coherente de la base de datos.
# 2,5 GB/día/nodo: comprimir y retener N copias fuera del VPS.
set -eu
STAMP=$(date +%Y%m%d_%H%M%S)
DEST=/var/backups/monitoreo/monitoreo_$STAMP.db.gz
sqlite3 /opt/monitoreo/dashboard/data/monitoreo.db ".backup '/tmp/monitoreo_$STAMP.db'"
gzip -c /tmp/monitoreo_$STAMP.db > "$DEST"
rm /tmp/monitoreo_$STAMP.db
find /var/backups/monitoreo -name 'monitoreo_*.db.gz' -mtime +14 -delete
```

Cron diario: `17 3 * * * /opt/monitoreo/backup.sh`. Luego `rsync`/`rclone` a un
almacén externo (el disco del VPS no es backup).

## Apéndice D — Pruebas de ingesta por red sin ESP32

```bash
# 1. El servidor responde
curl -s https://monitoreo.ejemplo.org/api/health

# 2. Una muestra de MPU6050 con ts_ms del sensor (contrato §4.1)
curl -s -X POST https://monitoreo.ejemplo.org/api/ingest \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer TOKEN-DEL-NODO' \
  -d '{"sensor":"mpu6050","ts_ms":1761000000000,
       "accel_x":0.121,"accel_y":-0.043,"accel_z":9.802,
       "gyro_x":0.00123,"gyro_y":-0.00045,"gyro_z":0.00012,"temp_c":26.31}'
# respuesta esperada: {"ok":true,"derived":{...}}

# 3. Una muestra de ADXL345 (solo aceleración; el resto es opcional)
curl -s -X POST https://monitoreo.ejemplo.org/api/ingest \
  -H 'Content-Type: application/json' \
  -d '{"sensor":"adxl345","ts_ms":1761000000020,
       "accel_x":0.020,"accel_y":-0.011,"accel_z":9.798}'

# 4. El dashboard la muestra: abrir https://monitoreo.ejemplo.org/
#    o consultar el histórico:
curl -s 'https://monitoreo.ejemplo.org/api/history?sensor=mpu6050&seconds=300'
```

Hoy la cabecera `Authorization` se ignora (§3.2); cuando se implemente, sin ella
la respuesta será 401.

## Apéndice E — Checklist antes de exponer el sistema

- [ ] Token Bearer por nodo implementado y probado (401 sin token).
- [ ] TLS con certificado válido; el ESP32 valida la CA (sin `setInsecure`).
- [ ] Dashboard y `/api/export.csv` detrás de basic_auth o Tailscale.
- [ ] Backend escuchando solo en `127.0.0.1`; firewall solo 80/443(/22).
- [ ] Cambiadas las credenciales de fábrica del router 4G y del WiFi; WPS off.
- [ ] Backup diario funcionando y **restaurado una vez de prueba**.
- [ ] Reloj del VPS sincronizado (NTP): `timedatectl` sin desviación.
- [ ] Sondeo de `/api/health` con alerta si no responde.
- [ ] Política de retención definida antes de que `monitoreo.db` llene el disco.
