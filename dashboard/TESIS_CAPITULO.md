# Capítulo — Plataforma de monitoreo estructural: arquitectura, protocolo y métricas derivadas

> Texto redactado en tercera persona, listo para integrar en la memoria de
> tesis. Las fórmulas usan notación LaTeX. Las referencias a
> *el contrato de datos* remiten al documento de diseño del repositorio
> (`CONTRATO_DE_DATOS.md`), que puede citarse como anexo de especificación.

---

## 1. Introducción

El monitoreo de una torre de estación base de red troncal rural exige
instrumentar el elemento estructural con un nodo de sensado capaz de capturar su
respuesta dinámica —vibración, inclinación y temperatura— y de entregar esa
información de forma continua a un operador. Este capítulo describe la
plataforma de software que cierra ese ciclo: desde el nodo sensor basado en
ESP32 hasta la interfaz de visualización, pasando por el protocolo de
comunicación, el almacenamiento y el cálculo de las métricas derivadas que
alimentarán los umbrales de detección de anomalías.

La plataforma se diseñó bajo tres premisas condicionadas por el escenario de
despliegue. En primer lugar, el tramo rural objeto de estudio dispone de
conectividad inalámbrica escasa e intermitente, por lo que **ningún componente
de la cadena puede depender de servicios externos ni de Internet**. En segundo
lugar, la electrónica y el firmware debían poder validarse con independencia de
la red, de modo que la primera puesta en marcha no mezclara dos fuentes de
fallo distintas. En tercer lugar, el sistema se concibe como una base
experimental reutilizable: las métricas que hoy se visualizan son las mismas
que mañana se someterán a detección de anomalías y, en su caso, a predicción.

## 2. Arquitectura de la plataforma

### 2.1 Organización en capas

La plataforma se estructura en cinco capas encadenadas —nodo sensor, ingesta y
parseo, procesamiento, persistencia y API, y presentación— de modo que cada una
solo conoce la interfaz de la capa inferior. Esta separación permite sustituir
un componente sin reescribir el resto; en particular, la elección del almacén
de datos es reversible por construcción (sección 4).

```
  Nodo sensor (ESP32)  →  Ingesta/parseo  →  Procesamiento  →  Persistencia
                                                                  ↓
                            Presentación (dashboard)  ←  API REST + WebSocket
```

El nodo sensor muestrea a 50 Hz y emite telemetría en texto delimitado por
salto de línea. La ingesta lee ese flujo, separa las líneas de telemetría de
las de diagnóstico y descarta lo que no pertenece al protocolo. El
procesamiento convierte cada muestra en un conjunto de métricas derivadas
magnitud, orientación y vibración. La persistencia guarda tanto las magnitudes
físicas como el registro crudo para auditoría. Finalmente, la API entrega los
datos en tiempo real por WebSocket y el histórico por HTTP al dashboard web.

### 2.2 Nodo sensor

El nodo está construido sobre un ESP32 con un MPU6050 (acelerómetro triaxial y
giroscopio triaxial, dirección I2C 0x68) como sensor principal de la respuesta
dinámica, complementado con un ADXL345 (acelerómetro de respaldo), un HX711
con celda de carga, un DS18B20 de temperatura, y entradas analógicas para
anemómetro y veleta. El MPU6050 se configura en rango de ±4 g y ±500 °/s con
filtro paso-banda de 21 Hz, valores acordes con las amplitudes esperadas en una
estructura rígida de torre y con la tasa de muestreo de 50 Hz.

Una decisión de diseño relevante del firmware es la **validación física de cada
muestra antes de publicarla**. La biblioteca de controlador del MPU6050 no
reporta errores de bus: si una lectura falla, devuelve los valores de un búfer
no inicializado. Para evitar que esa basura se interprete como telemetría, el
firmware verifica que la magnitud de la aceleración total se mantenga en el
rango 0,5–50 m/s² y la del giroscopio por debajo de 12 rad/s, y solo entonces
emite la línea de datos; en caso contrario reporta el problema como evento de
diagnóstico. Del mismo modo, un ping periódico al bus I2C cada 25 muestras
detecta un cable suelto sin penalizar el camino caliente de muestreo. Estas
precauciones responden a una regla general del sistema: **es preferible un
hueco en la serie temporal a un dato corrupto**, porque un hueco es visible y
un dato corrupto se disfraza de señal real.

### 2.3 Protocolo de comunicación

El protocolo es de texto, UTF-8, con líneas terminadas en `\n` a 115200
baudios 8N1. Su especificación completa es normativa y se recoge en el contrato
de datos del proyecto; aquí se resumen sus reglas de diseño.

**Telemetría.** Cada muestra de la IMU se emite como una línea delimitada por
comas:

```text
DATA,ax,ay,az,gx,gy,gz,temp
ADXL,ax,ay,az
```

con la aceleración en m/s², el giro en rad/s y la temperatura en °C. La
precisión decimal está fijada en el formato de emisión (`%.3f` para
aceleración, `%.5f` para giro, `%.2f` para temperatura), lo que acota el
tamaño de cada trama y evita notación exponencial. Se eligió texto delimitado
por comas y no un formato binario o JSON por tres razones: la depuración
directa con cualquier monitor serie, el coste nulo de parseo en el extremo
embebido, y la legibilidad de las capturas que se reproducen en la memoria de
la tesis. El ancho de banda resultante —del orden de 60 bytes por muestra a
50 Hz, unos 3 kB/s— está muy por debajo de la capacidad del enlace serie.

**Diagnóstico.** Toda línea que comienza con `[` es texto legible dirigido al
operador (detección de sensores, errores de bus, lecturas ambientales) y se
registra como *evento*, no como muestra. Esta distinción es importante: un
mensaje de error no debe contaminar ningún agregado estadístico de la señal.

**Robustez del receptor.** El parser ignora toda línea que no comience con
`DATA,`, `ADXL,` ni `[`, lo que descarta automáticamente los restos del
arranque del ESP32 —incluidas las tramas del bootloader a 74880 baudios— sin
reglas especiales. Una línea mal formada, o un campo que no sea un número
finito, causa el descarte de la línea completa y se contabiliza como error de
parseo sin interrumpir la ingesta. Si durante 2 segundos no llega ninguna
muestra válida, el servidor marca el estado de conexión como caído y lo
comunica a los clientes, recuperándose automáticamente al volver la
telemetría. El resultado es que un reinicio del nodo sensor —habitual durante
las pruebas con hardware— reduce la operación a la pérdida de unas muestras,
nunca a la caída del sistema.

### 2.4 Ingesta doble: USB serie y HTTP

La plataforma admite dos caminos de ingesta que convergen en el mismo
procesamiento y el mismo almacenamiento:

| Camino | Escenario | Justificación |
|---|---|---|
| USB serie (115200) | laboratorio con el nodo cableado | valida electrónica y firmware sin depender de la red |
| `POST /api/ingest` (HTTP) | despliegue rural sin cable | punto de convergencia para Wi-Fi, pasarela o enlace de largo alcance |

El camino serie es el que permite caracterizar el nodo sensor de forma
aislada —sensor, firmware y cableado— antes de introducir la variable radio.
El camino HTTP es el que usará el despliegue real, donde cada nodo (o su
pasarela) envía las mismas muestras en formato JSON. El sistema no registra ni
expone el camino por el que llegó cada dato: la telemetría es independiente de
su transporte, y así puede compararse sin sesgos entre campañas de laboratorio
y campañas de campo.

### 2.5 Entrega de datos en tiempo real

Para la visualización en vivo se implementó un canal **WebSocket** (`/ws`) con
tres tipos de mensaje —muestra, cambio de estado y evento—, todos ellos objetos
JSON definidos en el contrato. Se compararon tres alternativas:

* **Sondeo HTTP periódico**: de implementación trivial, pero a 50 Hz por sensor
  obliga a peticiones casi continuas, con latencia media de medio periodo de
  sondeo y una carga de cabeceras HTTP desproporcionada respecto de los datos
  transportados. Para una señal cuyo interés está en los transitorios de
  vibración, demorar la visualización es perder la información relevante.
* **Server-Sent Events (SSE)**: resuelve el envío servidor→cliente con menor
  complejidad que WebSocket, pero deja el sentido cliente→servidor fuera del
  canal, lo que obliga a un segundo mecanismo para las alertas y comandos
  previstos (confirmación de alarmas, solicitud de recalibración).
* **WebSocket (elegido)**: canal único, bidireccional y persistente, con una
  trama por mensaje. El servidor publica cada muestra a todos los clientes
  conectados, de modo que añadir espectadores no incrementa la carga de
  ingesta, y la bidireccionalidad deja resuelto de antemano el sentido de las
  alertas futuras.

El diseño asíncrono garantiza además que la ingesta de una muestra a 50 Hz no
bloquee la respuesta HTTP: la lectura serie se realiza fuera del bucle de
servicio y la persistencia se ejecuta sin retener las peticiones de los
clientes. La interfaz web, por su parte, está construida con HTML, CSS y
JavaScript local —sin herramientas de compilación, sin frameworks y sin
recursos externos— de modo que funcione íntegramente sin conexión a Internet,
requisito impuesto por el entorno de despliegue.

### 2.6 Interfaz de visualización

El dashboard concentra cuatro grupos de información: el estado de la cadena de
adquisición (conexión, puerto y presencia y contadores de cada sensor), la
telemetría cruda (aceleración, giro y temperatura), las métricas derivadas
(orientación y vibración) y el registro de eventos del nodo. Complementa la
vista en vivo con una consulta de histórico por ventana temporal y con la
exportación en CSV del rango seleccionado, pensada para el análisis posterior
en hojas de cálculo o en el entorno de cómputo numérico. La representación de
la orientación comparte convención de ejes y filtro de fusión con el
visualizador tridimensional implementado en Processing, de modo que ambas
vistas sirvan de contraste mutuo.

## 3. Almacenamiento de datos

### 3.1 Esquema

Los datos se persisten en una base de datos relacional SQLite contenida en un
único archivo. El esquema consta de tres tablas:

```sql
CREATE TABLE samples (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms     INTEGER NOT NULL,          -- epoch UTC en milisegundos
  sensor    TEXT    NOT NULL,          -- 'mpu6050' | 'adxl345' | ...
  accel_x   REAL, accel_y REAL, accel_z REAL,
  gyro_x    REAL, gyro_y  REAL, gyro_z REAL,
  temp_c    REAL,
  payload   TEXT                       -- registro crudo, para auditoría
);
CREATE INDEX idx_samples_sensor_ts ON samples(sensor, ts_ms);

CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL, sensor TEXT,
  level TEXT NOT NULL, code TEXT, message TEXT NOT NULL
);

CREATE TABLE sensor_status (
  sensor TEXT PRIMARY KEY, present INTEGER NOT NULL DEFAULT 0,
  last_ts_ms INTEGER, n_samples INTEGER NOT NULL DEFAULT 0
);
```

Tres decisiones conviene justificar. La marca de tiempo `ts_ms` es un entero
con epoch UTC en milisegundos: se resuelve con holgura el periodo de 20 ms de
la tasa de 50 Hz, y el formato es directo para ordenar, graficar y exportar
sin conversiones de zona horaria. La columna `payload` conserva el registro
crudo de cada muestra, de modo que cualquier algoritmo futuro de procesamiento
puede reprocesarse sobre los datos originales y no sobre una interpretación ya
fijada. Y el estado de cada sensor se materializa en `sensor_status`, en lugar
de derivarse con consultas de agregación sobre millones de filas, porque se
consulta en cada conexión de cliente y se actualiza de forma incremental.

### 3.2 Convención de unidades

Una regla recorre todo el sistema y merece explicitarse: **la aceleración se
almacena siempre en m/s² y la velocidad angular siempre en rad/s**; la
conversión a grados se realiza exclusivamente en la capa de presentación. Esta
convención elimina una clase entera de errores de escala —en particular el
factor de conversión de 57,2958 entre radianes y grados— que es frecuente
cuando la misma magnitud convive en dos unidades dentro de un mismo almacén.

### 3.3 Elección del motor de almacenamiento y ruta de migración

La elección de SQLite frente a un sistema de series temporales como InfluxDB
responde a un análisis de coste-beneficio acorde con la escala del trabajo.
SQLite no requiere servidor, configuración ni autenticación; la base de datos es
un archivo portable que se copia, se archiva como evidencia experimental y se
abre en cualquier equipo, lo que favorece la reproducibilidad del trabajo
académico; y ofrece garantías transaccionales completas. Para el volumen de un
único nodo —100 muestras por segundo, unos 8,6 millones de registros diarios— y
con un índice sobre `(sensor, ts_ms)` que cubre exactamente el patrón de
consulta por ventana temporal, el rendimiento es más que suficiente.

Un sistema de series temporales habría añadido un servicio independiente
(frecuentemente contenedorizado), una curva de aprendizaje adicional en su
lenguaje de consulta y su modelo de esquema, y una superficie operativa que en
campo constituye un punto de fallo más. Su uso se justifica, en cambio, cuando
concurran algunas de estas condiciones: despliegue de múltiples nodos con
retención de meses, agregación continua de datos (estadísticos por hora sobre
todo el histórico), escritura concurrente desde varios procesos o requisitos de
alta disponibilidad. Para hacer esa migración reversible sin reescribir el
sistema, todo acceso a datos pasa por una capa de repositorio que concentra las
consultas: el resto del software desconoce SQLite y solo conoce la interfaz del
repositorio, de modo que sustituir el almacén se reduce a reimplementar esa
interfaz sobre la nueva tecnología.

### 3.4 Crecimiento y retención

A 100 muestras por segundo con registro crudo, el archivo crece entre 1,5 y 2,5
GB por día. La política prevista de crecimiento es: (i) materializar
agregados horarios —media, RMS y pico por ventana— y purgar el detalle más
allá de un horizonte configurable; (ii) particionar por tiempo si se migra a
un motor relacional orientado a series; (iii) adoptar InfluxDB o
PostgreSQL+TimescaleDB cuando se cumplan las condiciones de la sección 3.3. La
exportación CSV permite, en todo caso, desacoplar el análisis a largo plazo del
almacén operativo.

## 4. Procesamiento de señales y métricas derivadas

El procesamiento se realiza en una única capa del servidor, decisión que
responde a la necesidad de que la visualización, la exportación y la futura
detección de anomalías compartan exactamente los mismos valores. Las
magnitudes siguientes se definen en el contrato de datos; `g = 9,80665 m/s²`.

### 4.1 Magnitudes instantáneas

La magnitud de la aceleración y del giro se definen como la norma euclídea del
vector correspondiente:

$$
a_{mag} = \sqrt{a_x^2 + a_y^2 + a_z^2} \qquad
\omega_{mag} = \sqrt{\omega_x^2 + \omega_y^2 + \omega_z^2}
$$

La primera es útil como indicador escalar del estado de movimiento —en reposo
hidrostático vale aproximadamente `g`— y la segunda como medida de intensidad
de la rotación independiente de la orientación del sensor.

### 4.2 Orientación: alabeo, cabeceo y guiñada

El alabeo (roll) y el cabeceo (pitch) se estiman a partir de la dirección de la
gravedad medida por el acelerómetro:

$$
\phi_{acc} = \operatorname{atan2}(a_y, a_z) \qquad
\theta_{acc} = \operatorname{atan2}\!\left(-a_x,\ \sqrt{a_y^2 + a_z^2}\right)
$$

expresados en grados. Esta estimación es exacta en reposo pero ruidosa ante
vibración, mientras que la integración del giroscopio es suave a corto plazo y
deriva a largo plazo. Por ello ambas se fusionan con un **filtro
complementario** de constante $\alpha = 0{,}96$:

$$
\phi_t = \alpha \left( \phi_{t-1} + \omega_x \Delta t \right) + (1-\alpha)\,\phi_{acc}
$$

y análogamente para $\theta$. El parámetro $\alpha$ pondera la confianza en el
giroscopio a corto plazo y en el acelerómetro a largo plazo; su valor coincide
con el del visualizador tridimensional del proyecto, de modo que ambas
representaciones de la orientación sean consistentes entre sí.

La guiñada (yaw) no es observable a partir de la gravedad, por lo que se obtiene
exclusivamente por integración de la velocidad angular del eje Z con el sesgo
(bias) del giroscopio corregido:

$$
\psi_t = \mathrm{wrap}_{(-180,\,180]}\!\left( \psi_{t-1} + (\omega_z - b_z)\,\Delta t \cdot \frac{180}{\pi} \right)
$$

El sesgo $b_z$ se estima al arranque manteniendo el nodo en reposo durante
aproximadamente un segundo (unas 50 muestras), tal como hace también el
visualizador de Processing. Consecuentemente, el yaw es fiable para medir
cambios relativos de orientación pero deriva con el tiempo, limitación
inherente a la ausencia de un magnetómetro que lo ancle.

### 4.3 Métricas de vibración

La señal base para la detección estructural es la aceleración de vibración,
entendida como la aceleración total una vez retirada la gravedad:

$$
v(t) = \left\| \mathbf{a}(t) \right\| - g
$$

Sobre una **ventana de 1 segundo** —50 muestras a la tasa de 50 Hz— se calculan
su raíz cuadrática media y su pico:

$$
v_{rms} = \sqrt{\frac{1}{N} \sum_{i=1}^{N} v(t_i)^2}
\qquad
v_{peak} = \max_{i} v(t_i)
$$

La ventana de 1 segundo es un compromiso deliberado: es corta frente al período
de los modos estructurales de interés de la torre y larga frente al intervalo
de muestreo, de modo que el RMS estabiliza la estimación de energía sin
perder resolución temporal. El RMS integra la energía de la vibración y es
robusto frente al ruido puntual; el pico conserva la información de los
transitorios. Ambas magnitudes se calculan en el mismo punto del sistema para
los dos acelerómetros (MPU6050 y ADXL345), y son las que se emplearán como
señal de referencia de los umbrales de detección de anomalías del capítulo
correspondiente. Los transitorios de aceleración que superen el umbral calibrado
sobre $v_{rms}$ constituyen, por tanto, la definición operativa de evento
estructural de esta plataforma.

## 5. Justificación de las decisiones de diseño

El cuadro siguiente resume las decisiones y su motivo, de modo que cada una
pueda evaluarse de forma independiente:

| Decisión | Alternativa descartada | Motivo |
|---|---|---|
| Python + FastAPI | Node.js, Go, C++ | el volumen (≈100 muestras/s) no exige rendimiento crudo; a cambio se obtiene WebSocket integrado, PySerial y continuidad con numpy/scikit-learn para la etapa de detección sin cambiar de lenguaje |
| SQLite + capa de repositorio | InfluxDB, PostgreSQL desde el inicio | cero configuración, archivo único portable y transaccional, suficiente a escala de tesis; el repositorio hace reversible la decisión |
| WebSocket | sondeo HTTP, SSE | latencia baja, *broadcast* a varios clientes y canal bidireccional para alertas futuras |
| Ingesta USB serie + HTTP | solo red | permite validar electrónica y firmware sin depender de una red inalámbrica escasa, y separa las fuentes de fallo |
| Texto CSV serie | binario, JSON | depuración directa, coste nulo en el embebido, legibilidad en la memoria |
| Métricas derivadas en el servidor | calcular en el navegador | una sola definición compartida por visualización, exportación y detección |
| Validación física en el firmware | confiar en el controlador | un bus I2C degradado produce valores creíbles; el filtro por rango evita datos falsos |
| Frontend sin build ni CDN | framework + empaquetado | operación garantizada sin Internet y ciclo de modificación sin herramientas de compilación |
| RMS de vibración en ventana de 1 s | pico instantáneo, RMS más largo | robustez del RMS con resolución temporal suficiente para la dinámica de interés |

## 6. Limitaciones y trabajo futuro

Conviene declarar con franqueza las limitaciones del diseño actual, que al
mismo tiempo definen el trabajo futuro:

* **Ausencia de autenticación y cifrado.** El servidor opera en red local de
  confianza y escucha en la interfaz de bucle local. Un despliegue real exige
  HTTPS/WSS, autenticación por nodo y control de acceso a la exportación de
  datos.
* **Un solo nodo de telemetría.** Ni el esquema de datos ni el estado en
  memoria distinguen emisores. La extensión prevista añade un identificador de
  nodo a las tablas y a los mensajes, y trata cada emisor con su propio estado
  de conexión.
* **Sincronización temporal por llegada.** La marca de tiempo de cada muestra
  es la hora de recepción salvo que el emisor aporte la suya. Esto es adecuado
  para un nodo y para visualización, pero insuficiente para análisis modal
  coordinado entre varios puntos de la estructura, donde será necesario un
  sincronismo real (NTP, PTP o reloj de tiempo real en cada nodo).
* **Un único escritor en la base de datos.** SQLite serializa las escrituras;
  la concurrencia de varios procesos de ingesta obligaría a migrar el almacén.
* **Ausencia de alertas automáticas.** El canal de comunicación está preparado
  para ellas, pero la política de umbrales y la notificación pertenecen a la
  etapa de detección de anomalías.
* **Métricas derivadas recalculadas al servir.** Para históricos extensos
  convendrá materializarlas en agregados horarios.
* **Deriva del yaw.** Al no disponer de magnetómetro, la guiñada solo es
  interpretable como variación relativa respecto del arranque.

En cuanto al trabajo futuro, la plataforma está construida para acogerlo sin
cambios estructurales: la detección de anomalías sobre las series de
$v_{rms}$ y $v_{peak}$, el entrenamiento de modelos de predicción en el mismo
entorno de Python, la ampliación a una red de nodos sincronizados sobre la
torre, y la migración del almacenamiento a una base de series temporales cuando
el volumen de datos lo justifique.

---

## Nota de coherencia con el repositorio

Este capítulo describe la plataforma tal como queda especificada en el contrato
de datos del proyecto. Donde el capítulo menciona magnitudes concretas
($\alpha = 0{,}96$, $g = 9{,}80665$ m/s², ventana de 1 s, tasa de 50 Hz,
rangos ±4 g y ±500 °/s), los valores coinciden literalmente con el contrato de
datos y con el firmware del nodo sensor; cualquier modificación futura de esos
valores debe reflejarse primero en el contrato y después en este texto.
