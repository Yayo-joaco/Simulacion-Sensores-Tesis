/*
 * PRUEBA FISICA - TODOS LOS SENSORES
 * Tesis: IoT-WSN y CCTV para monitoreo de subsistemas criticos
 *
 * Sensores soportados:
 * 1. MPU6050  (I2C 0x68) - Acelerometro/Giroscopio  -> linea DATA a 50 Hz
 * 2. ADXL345  (I2C 0x53) - Acelerometro             -> linea ADXL a 50 Hz
 * 3. HX711    (GPIO 4/5) - Celda de carga
 * 4. DS18B20  (GPIO 15)  - Temperatura
 * 5. Anemometro (GPIO 34) - Velocidad viento (analogico)
 * 6. Veleta    (GPIO 35) - Direccion viento (analogico)
 *
 * Conexion ESP32:
 *   I2C:  SDA=21, SCL=22
 *   HX711: DT=GPIO4, SCK=GPIO5
 *   DS18B20: DQ=GPIO15
 *   Anemometro: Signal=GPIO34
 *   Veleta: Signal=GPIO35
 *
 * Protocolo serie (115200 baud) - ver dashboard/CONTRATO_DE_DATOS.md seccion 2:
 *   DATA,ax,ay,az,gx,gy,gz,temp    -> 50 Hz, accel m/s^2, gyro rad/s, temp C
 *   ADXL,ax,ay,az                  -> 50 Hz, accel m/s^2 (Adafruit_ADXL345_U
 *                                      ya devuelve m/s^2, sin conversion)
 *   [SENSOR] ...                   -> diagnostico cada 2 s
 *
 * PRESUPUESTO SERIE (verificado con numeros):
 *   A 11520 baudios 8N1 hay ~11520 B/s utiles de enlace.
 *   Linea DATA tipica  ~= 55 B (incluye \n)
 *   Linea ADXL tipica  ~= 24 B (incluye \n)
 *   Ambos a 50 Hz: (55 + 24) * 50 = 3950 B/s = ~34% del enlace.
 *   Incluso con valores negativos largos (lineas ~10% mas grandes) y los
 *   diagnosticos [..] cada 2 s, se queda por debajo del 40%: cabe de sobra.
 *   Por eso el ADXL se mantiene a 50 Hz como exige el contrato; NO hace falta
 *   bajarlo a 25 Hz. Si algun dia se activan HX711 + anemometro a 50 Hz habria
 *   que recalcular, pero hoy el margen es ~7500 B/s libres.
 *
 * MEJORAS DE ROBUSTEZ (revision 2026-09):
 *  - Serial.setTxBufferSize(512): los prints de diagnostico no bloquean el muestreo.
 *  - Wire.setTimeOut(10): un bus I2C trabado cuesta 10 ms, no 150 ms.
 *  - MPU6050: ping I2C cada 25 muestras + validacion de rango fisico; si falla
 *    NO se emite DATA y se avisa por serie (evita basura de pila como dato valido).
 *  - MPU6050: re-deteccion automatica si no estaba conectado al arrancar.
 *  - ADXL345: lineas ADXL a 50 Hz con la misma validacion de rango fisico que
 *    DATA (magnitud > 200 m/s^2 o NaN => no se emite y se cuenta el error).
 *  - HX711: pin DT con pull-up interno + espera con timeout. IMPORTANTE: la
 *    libreria bogde/HX711 llama a wait_ready() sin timeout dentro de read(),
 *    por lo que puede congelar el bucle si el sensor se desconecta en caliente.
 *    Por eso ENABLE_HX711 esta en 0 para la prueba en vivo MPU6050+Processing.
 *  - DS18B20: sondeo de conversion limitado a 50 Hz y validacion de lecturas
 *    imposibles (DEVICE_DISCONNECTED_C / 85.0 C de arranque).
 */

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_ADXL345_U.h>
#include <HX711.h>
#include <OneWire.h>
#include <DallasTemperature.h>

// === OPCIONES DE COMPILACION ===
// Activa solo lo que vayas a probar. El camino caliente del MPU6050 se beneficia
// de tener el minimo de sensores diagnosticando.
#define ENABLE_ADXL345 1
#define ENABLE_HX711   0   // 1 solo con el HX711 conectado (ver nota de arriba)
#define ENABLE_DS18B20 1
#define ENABLE_ANEMO   1

// === OBJETOS ===
Adafruit_MPU6050 mpu;
#if ENABLE_ADXL345
Adafruit_ADXL345_Unified adxl = Adafruit_ADXL345_Unified(12345);
#endif
#if ENABLE_HX711
HX711 scale;
#endif
#if ENABLE_DS18B20
OneWire oneWire(15);
DallasTemperature ds18b20(&oneWire);
#endif

// === ESTADO ===
bool mpuFound = false;
uint8_t mpuAddr = 0x68;
bool adxlFound = false;
bool hx711Ready = false;
bool ds18b20Ready = false;
bool ds18b20ConversionPending = false;

// === PINES ===
#define HX711_DT  4
#define HX711_SCK 5
#define DS18B20_DQ 15
#define ANEMOMETER_PIN 34
#define VELETA_PIN 35

// === LIMITES FISICOS PARA VALIDAR MUESTRAS DEL MPU6050 ===
// Aceleracion total entre 0.5 y 50 m/s^2 (en reposo ~9.81; solo basura de pila
// o un bus roto salen de este rango de forma sostenida).
#define ACCEL_MAG_MIN 0.5f
#define ACCEL_MAG_MAX 50.0f
// Giroscopio: el MPU6050 a +-500 dps no puede superar ~8.7 rad/s; dejamos margen.
#define GYRO_MAG_MAX  12.0f

// === LIMITES FISICOS PARA VALIDAR MUESTRAS DEL ADXL345 ===
// El contrato (seccion 2.1) exige descartar la linea completa si la magnitud de
// aceleracion es absurda: > 200 m/s^2 o NaN. A +-4 g el sensor no pasa de
// ~39.3 m/s^2 en uso normal; 200 deja margen para impactos reales de la torre
// sin aceptar basura de un bus I2C degradado.
#define ADXL_ACCEL_MAG_MAX 200.0f

static bool mpuSampleValid(const sensors_event_t &a, const sensors_event_t &g,
                           float *amOut = nullptr, float *gmOut = nullptr) {
  float am = sqrt(a.acceleration.x * a.acceleration.x
                + a.acceleration.y * a.acceleration.y
                + a.acceleration.z * a.acceleration.z);
  float gm = sqrt(g.gyro.x * g.gyro.x + g.gyro.y * g.gyro.y + g.gyro.z * g.gyro.z);
  if (amOut) *amOut = am;
  if (gmOut) *gmOut = gm;
  // La comparacion con NaN devuelve false, asi que tambien descarta NaN.
  return (am > ACCEL_MAG_MIN && am < ACCEL_MAG_MAX && gm < GYRO_MAG_MAX);
}

static bool mpuBusAlive() {
  Wire.beginTransmission(mpuAddr);
  return (Wire.endTransmission() == 0);
}

#if ENABLE_ADXL345
// Mismo esquema de validacion que el MPU6050: getEvent() de la libreria Adafruit
// no reporta errores de I2C, asi que una lectura fallida puede parsear un buffer
// sin inicializar. Solo se emite ADXL con magnitud fisicamente posible.
static bool adxlSampleValid(const sensors_event_t &e, float *amOut = nullptr) {
  float am = sqrt(e.acceleration.x * e.acceleration.x
                + e.acceleration.y * e.acceleration.y
                + e.acceleration.z * e.acceleration.z);
  if (amOut) *amOut = am;
  // La comparacion con NaN/Inf devuelve false, asi que tambien se descartan.
  return (am < ADXL_ACCEL_MAG_MAX);
}
#endif

void setup() {
  // El buffer TX se configura ANTES de begin(): con el buffer por defecto (0)
  // cada print espera a que la FIFO de 128 B se vacie (~7 ms por rafaga).
  Serial.setTxBufferSize(512);
  Serial.begin(115200);
  delay(2000);

  Serial.println("================================================");
  Serial.println("  PRUEBA FISICA - TODOS LOS SENSORES");
  Serial.println("  Tesis IoT-WSN - Monitoreo Estructural");
  Serial.println("================================================");
  Serial.println();

  // --- I2C ---
  Wire.begin(21, 22);
  // Acota el bloqueo por transaccion: por defecto son 50 ms, y getEvent() hace
  // varias transacciones, o sea hasta ~150 ms congelado si el bus queda a masa.
  Wire.setTimeOut(10);
  Serial.println("[I2C] Bus iniciado (SDA=21, SCL=22, timeout=10 ms)");
  Serial.println("[SCAN] Buscando dispositivos I2C...");

  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  Encontrado: 0x%02X", addr);
      if (addr == 0x68) Serial.print(" (MPU6050)");
      if (addr == 0x53) Serial.print(" (ADXL345)");
      if (addr == 0x69) Serial.print(" (MPU6050 AD0=HIGH)");
      Serial.println();
    }
  }

  // --- MPU6050 ---
  Serial.println();
  Serial.println("[MPU6050] Inicializando...");
  if (mpu.begin(0x68)) {
    mpuFound = true; mpuAddr = 0x68;
  } else if (mpu.begin(0x69)) {
    mpuFound = true; mpuAddr = 0x69;
  }
  if (mpuFound) {
    mpu.setAccelerometerRange(MPU6050_RANGE_4_G);
    mpu.setGyroRange(MPU6050_RANGE_500_DEG);
    mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
    Serial.printf("[MPU6050] OK - Detectado en 0x%02X\n", mpuAddr);
  } else {
    Serial.println("[MPU6050] NO detectado - se reintentara cada 2 s");
  }

#if ENABLE_ADXL345
  // --- ADXL345 ---
  Serial.println();
  Serial.println("[ADXL345] Inicializando...");
  if (adxl.begin()) {
    adxlFound = true;
    adxl.setRange(ADXL345_RANGE_4_G);
    Serial.println("[ADXL345] OK - Detectado en 0x53");
  } else {
    Serial.println("[ADXL345] NO detectado");
  }
#endif

#if ENABLE_HX711
  // --- HX711 ---
  Serial.println();
  Serial.println("[HX711] Inicializando (DT=4, SCK=5)...");
  scale.begin(HX711_DT, HX711_SCK);
  // La libreria deja DOUT como INPUT (sin pull-up). Con el sensor ausente el pin
  // flota y puede leerse "listo". Con pull-up lee HIGH = no listo, y asi la guarda
  // de abajo nunca entra en la lectura bloqueante.
  pinMode(HX711_DT, INPUT_PULLUP);
  pinMode(HX711_SCK, OUTPUT);
  digitalWrite(HX711_SCK, LOW);
  delay(500);
  if (scale.wait_ready_timeout(300)) {
    hx711Ready = true;
    scale.set_scale(2280.f);  // Factor de calibracion (ajustar)
    scale.tare(1);            // 1 lectura, no 10: menos exposicion a bloqueo
    Serial.println("[HX711] OK - Sensor detectado");
    Serial.println("[HX711] Tara realizada");
  } else {
    Serial.println("[HX711] NO detectado - Verifique conexion");
  }
#endif

#if ENABLE_DS18B20
  // --- DS18B20 ---
  Serial.println();
  Serial.println("[DS18B20] Inicializando (DQ=GPIO15)...");
  ds18b20.begin();
  ds18b20.setWaitForConversion(false);
  int deviceCount = ds18b20.getDeviceCount();
  Serial.printf("[DS18B20] Dispositivos encontrados: %d\n", deviceCount);
  if (deviceCount > 0) {
    ds18b20Ready = true;
    Serial.println("[DS18B20] OK");
  } else {
    Serial.println("[DS18B20] NO detectado - Verifique conexion");
  }
#endif

#if ENABLE_ANEMO
  // --- ANEMOMETRO / VELETA ---
  Serial.println();
  Serial.println("[ANEMOMETRO] Pin analogico GPIO34");
  Serial.println("[VELETA] Pin analogico GPIO35");
  Serial.println("  (Lecturas analogicas - requieren sensor fisico)");
#endif

  Serial.println();
  Serial.println("================================================");
  Serial.println("  RESUMEN DE DETECCION:");
  Serial.printf("  MPU6050:   %s\n", mpuFound ? "OK" : "NO");
  Serial.printf("  ADXL345:   %s\n", adxlFound ? "OK" : "NO");
  Serial.printf("  HX711:     %s\n", hx711Ready ? "OK" : "NO");
  Serial.printf("  DS18B20:   %s\n", ds18b20Ready ? "OK" : "NO");
  Serial.println("  Anemometro: ANALOG (GPIO34)");
  Serial.println("  Veleta:     ANALOG (GPIO35)");
  Serial.println("================================================");
  Serial.println();
  Serial.println("--- MPU6050: 50 Hz | ADXL345: 50 Hz | Diagnostico otros: cada 2 segundos ---");
  Serial.println();
}

void loop() {
  static unsigned long lastMpuRead = 0;
  static unsigned long lastAdxlRead = 0;
  static unsigned long lastAuxRead = 0;
  static unsigned long lastDsPoll = 0;
  static uint16_t samplesSincePing = 0;
  static uint16_t badSamples = 0;
  static uint16_t adxlBadSamples = 0;
  unsigned long now = millis();

#if ENABLE_ADXL345
  // --- ADXL345 a 50 Hz: emisor de lineas ADXL ---
  // Este bloque va ANTES del MPU6050 a proposito: el bloque del MPU hace
  // `return` cuando descarta una muestra o el bus no responde, y si el ADXL
  // estuviera despues dejaria de emitirse cada vez que el MPU fallara,
  // acoplando dos streams que el contrato trata como independientes.
  // Coste: una transaccion I2C corta (<1 ms con bus sano; <=10 ms con el
  // Wire.setTimeOut(10) si el bus se cuelga). El gate del MPU se mide de flanco
  // a flanco (>=20 ms), por lo que la cadencia de DATA sigue siendo 50 Hz.
  if (adxlFound && now - lastAdxlRead >= 20) {
    lastAdxlRead = now;

    sensors_event_t ev;
    // Igual que el MPU: getEvent() no reporta errores I2C, por eso validamos el
    // rango fisico antes de publicar la linea ADXL.
    adxl.getEvent(&ev);

    float aam = 0.0f;
    if (!adxlSampleValid(ev, &aam)) {
      // Mismo esquema de diagnostico que el MPU6050: las 3 primeras descartes
      // muestran la magnitud recibida (0.000 o nan = transferencias fallando)
      // y luego se avisa cada 25 descartes para no inundar el enlace.
      if (++adxlBadSamples <= 3 || adxlBadSamples % 25 == 0) {
        Serial.printf("[ADXL345] MUESTRAS INVALIDAS - revise cableado I2C "
                      "(|a|=%.3f m/s2)\n", aam);
      }
    } else {
      adxlBadSamples = 0;
      // Formato del contrato: "ADXL,ax,ay,az", %.3f, m/s^2, terminada en \n.
      // Una sola llamada a printf = linea atomica (buffer TX de 512 B de sobra).
      Serial.printf("ADXL,%.3f,%.3f,%.3f\n",
                    ev.acceleration.x, ev.acceleration.y, ev.acceleration.z);
    }
  }
#endif

  // --- MPU6050 a 50 Hz: unico emisor de lineas DATA ---
  if (mpuFound && now - lastMpuRead >= 20) {
    lastMpuRead = now;

    // Ping I2C barato cada 25 muestras (0.5 s): detecta cable suelto sin
    // penalizar el camino caliente.
    if (++samplesSincePing >= 25) {
      samplesSincePing = 0;
      if (!mpuBusAlive()) {
        Serial.println("[MPU6050] I2C ERROR - sin respuesta en el bus");
        return;
      }
    }

    sensors_event_t a, g, temp;
    // getEvent() siempre devuelve true y no reporta errores de I2C: si la lectura
    // falla, la libreria parsea un buffer sin inicializar. Por eso validamos el
    // rango fisico de la muestra antes de publicarla como DATA.
    mpu.getEvent(&a, &g, &temp);

    float am = 0.0f, gm = 0.0f;
    if (!mpuSampleValid(a, g, &am, &gm)) {
      // Diagnostico de la descartada. "MUESTRAS INVALIDAS" con el modulo
      // DETECTADO y el ping OK no es falta de alimentacion (el LED solo prueba
      // VCC/GND): los DATOS llegan corruptos. Las magnitudes de la descartada
      // distinguen los fallos tipicos:
      //   |a| = 0.000 o nan -> transferencias I2C fallando: cables SDA/SCL/GND
      //   |a| en cientos    -> basura pura del bus: mismo origen
      //   |g| >= 12 rad/s   -> el acelerometro llega bien pero el giro no
      // Detalle solo en las 3 primeras descartes y luego cada 25 (2/s a 50 Hz):
      // diagnostico suficiente sin inundar el enlace serie ni la consola.
      if (++badSamples <= 3 || badSamples % 25 == 0) {
        Serial.printf("[MPU6050] MUESTRAS INVALIDAS - revise cableado I2C "
                      "(|a|=%.3f m/s2 |w|=%.5f rad/s)\n", am, gm);
      }
      return;
    }
    badSamples = 0;

    Serial.printf("DATA,%.3f,%.3f,%.3f,%.5f,%.5f,%.5f,%.2f\n",
                  a.acceleration.x, a.acceleration.y, a.acceleration.z,
                  g.gyro.x, g.gyro.y, g.gyro.z, temp.temperature);
  }

#if ENABLE_DS18B20
  // isConversionComplete() hace una operacion de bus 1-Wire por llamada, por eso
  // se limita a 50 Hz en lugar de barrr el bus a la velocidad de loop().
  if (ds18b20Ready && ds18b20ConversionPending && now - lastDsPoll >= 20) {
    lastDsPoll = now;
    if (ds18b20.isConversionComplete()) {
      float tempC = ds18b20.getTempCByIndex(0);
      // -127 = DEVICE_DISCONNECTED_C; 85.0 = valor de arranque del scratchpad.
      if (tempC <= DEVICE_DISCONNECTED_C + 0.5f || tempC >= 84.9f) {
        Serial.println("[DS18B20] ERROR: lectura invalida o sensor desconectado");
      } else {
        // Una sola línea, literalmente el formato del contrato §2.2
        // ("[DS18B20] Temperatura: 28.44 °C"). Si se emitiera el valor en una
        // segunda línea con sangría, esa línea no empezaría por '[' y el
        // parser la descartaría (§2.3): la temperatura del DS18B20 —sensor
        // sin líneas de telemetría— se perdería entera del registro.
        Serial.printf("[DS18B20] Temperatura: %.2f °C\n", tempC);
      }
      ds18b20ConversionPending = false;
    }
  }
#endif

  if (now - lastAuxRead < 2000) return;
  lastAuxRead = now;

  // --- Re-deteccion de sensores ausentes al arrancar ---
  if (!mpuFound) {
    if (mpu.begin(0x68)) { mpuFound = true; mpuAddr = 0x68; }
    else if (mpu.begin(0x69)) { mpuFound = true; mpuAddr = 0x69; }
    if (mpuFound) {
      mpu.setAccelerometerRange(MPU6050_RANGE_4_G);
      mpu.setGyroRange(MPU6050_RANGE_500_DEG);
      mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
      Serial.printf("[MPU6050] RECONECTADO en 0x%02X\n", mpuAddr);
    }
  }

#if ENABLE_ADXL345
  if (adxlFound) {
    sensors_event_t event;
    adxl.getEvent(&event);
    // Diagnóstico en UNA línea que empieza por '[': solo así el parser lo
    // registra como evento completo (§2.2). Partido en dos líneas, la segunda
    // (con sangría) se descarta por §2.3 y el valor nunca llega al servidor.
    Serial.printf("[ADXL345] Accel (m/s2): X=%.2f Y=%.2f Z=%.2f\n",
                  event.acceleration.x, event.acceleration.y, event.acceleration.z);
  }
#endif

#if ENABLE_HX711
  if (hx711Ready && scale.wait_ready_timeout(50)) {
    float reading = scale.get_units(1);
    // Una sola línea que empieza por '[' (§2.2): si el valor viajara en una
    // segunda línea con sangría, el parser la descartaría (§2.3) y el peso no
    // llegaría nunca a la tabla de eventos.
    Serial.printf("[HX711] Peso: %.2f g\n", reading);
  }
#endif

#if ENABLE_DS18B20
  if (ds18b20Ready && !ds18b20ConversionPending) {
    ds18b20.requestTemperatures();
    ds18b20ConversionPending = true;
    lastDsPoll = now;
  }
#endif

#if ENABLE_ANEMO
  int anemVal = analogRead(ANEMOMETER_PIN);
  int veletaVal = analogRead(VELETA_PIN);
  // Ambos valores en la MISMA línea de diagnóstico (§2.2): tres líneas
  // separadas solo producirían un evento '[ANEMOMETRO/VELETA]' sin cuerpo y
  // dos líneas con sangría que el parser descarta (§2.3).
  Serial.printf("[ANEMOMETRO/VELETA] Anemometro(raw)=%d Veleta(raw)=%d\n",
                anemVal, veletaVal);
#endif

  Serial.println();
}
