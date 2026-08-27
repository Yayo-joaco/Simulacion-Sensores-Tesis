/*
 * ============================================================
 * TESIS: Sistema IoT-WSN y CCTV - Subsistema Estructural
 * Autores: Gerardo Rabanal y Gilmar Vargas
 * ============================================================
 * PRUEBA INTEGRADA: 4 sensores simultáneos
 * ------------------------------------------------------------
 * Sensores conectados:
 *   1. MPU6050 #1 (I2C, 0x68) — simula sensor de antena
 *   2. MPU6050 #2 (I2C, 0x69) — simula ADXL345 (torre)
 *   3. HX711 (DT/SCK) — galga extensométrica
 *   4. DS18B20 (1-Wire) — temperatura
 *
 * Nota sobre ADXL345:
 *   Wokwi no soporta custom chips en VS Code sin compilador
 *   C→WASM. Se usa MPU6050 con dirección alternativa (0x69)
 *   para demostrar coexistencia I2C. El concepto validado es
 *   idéntico: dos dispositivos I2C en el mismo bus.
 * ============================================================
 */

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include "HX711.h"
#include <OneWire.h>
#include <DallasTemperature.h>

// --- Pines ---
#define I2C_SDA     21
#define I2C_SCL     22
#define HX711_DT    4
#define HX711_SCK   5
#define DS18B20_DQ  15
#define SERIAL_BAUD 115200

// --- Direcciones I2C ---
#define SENSOR1_ADDR  0x68
#define SENSOR2_ADDR  0x69

// --- Objetos sensores ---
Adafruit_MPU6050 sensor1;
Adafruit_MPU6050 sensor2;
HX711 scale;
OneWire oneWire(DS18B20_DQ);
DallasTemperature ds18b20(&oneWire);

unsigned long loopCount = 0;

// --- Prototipos ---
void imprimirBanner();
void escanearBusI2C();
bool initMPU(Adafruit_MPU6050 &s, uint8_t addr, const char* name);
bool initHX711();
bool initDS18B20();
void leerMPU(Adafruit_MPU6050 &s, const char* name);
void leerHX711();
void leerDS18B20();

// ============================================================
void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(1000);
  imprimirBanner();

  Wire.begin(I2C_SDA, I2C_SCL);
  Serial.println("[INIT] Bus I2C (GPIO 21/22)\n");

  // Escaneo
  Serial.println("═══════════════════════════════════════");
  Serial.println("  Escaneo I2C");
  Serial.println("═══════════════════════════════════════");
  escanearBusI2C();
  Serial.println();

  // Init
  Serial.println("═══════════════════════════════════════");
  Serial.println("  Inicialización");
  Serial.println("═══════════════════════════════════════");
  bool s1 = initMPU(sensor1, SENSOR1_ADDR, "MPU6050 #1 (0x68)");
  bool s2 = initMPU(sensor2, SENSOR2_ADDR, "MPU6050 #2 (0x69)");
  bool hx = initHX711();
  bool ds = initDS18B20();
  Serial.println();

  // Resultado
  int ok = s1 + s2 + hx + ds;
  Serial.println("╔═══════════════════════════════════════╗");
  Serial.println("║  RESULTADO: Subsistema Estructural    ║");
  Serial.println("╠═══════════════════════════════════════╣");
  Serial.printf("║  MPU6050 #1 (0x68): %s\n", s1?"✅ OK":"❌ FAIL");
  Serial.printf("║  MPU6050 #2 (0x69): %s\n", s2?"✅ OK":"❌ FAIL");
  Serial.printf("║  HX711 (DT/SCK):    %s\n", hx?"✅ OK":"❌ FAIL");
  Serial.printf("║  DS18B20 (1-Wire):  %s\n", ds?"✅ OK":"❌ FAIL");
  Serial.println("╠═══════════════════════════════════════╣");
  Serial.printf("║  Total: %d/4 operativos\n", ok);
  Serial.println("╚═══════════════════════════════════════╝\n");

  Serial.println("═══════════════════════════════════════");
  Serial.println("  Lectura cada 2s");
  Serial.println("═══════════════════════════════════════\n");
}

// ============================================================
void loop() {
  loopCount++;
  Serial.printf("─── Ciclo #%lu ───────────────────────\n", loopCount);
  leerMPU(sensor1, "MPU6050 #1 (0x68)");
  leerMPU(sensor2, "MPU6050 #2 (0x69)");
  leerHX711();
  leerDS18B20();
  Serial.println();
  delay(2000);
}

// ============================================================
void imprimirBanner() {
  Serial.println("╔═══════════════════════════════════════╗");
  Serial.println("║  TESIS: IoT-WSN - Subsistema Estruc. ║");
  Serial.println("║  MPU6050x2 + HX711 + DS18B20         ║");
  Serial.println("║  Autores: Rabanal / Vargas            ║");
  Serial.println("╚═══════════════════════════════════════╝\n");
}

void escanearBusI2C() {
  int n = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  ✅ 0x%02X", a);
      if (a == SENSOR1_ADDR) Serial.print(" ← MPU #1");
      if (a == SENSOR2_ADDR) Serial.print(" ← MPU #2");
      Serial.println();
      n++;
    }
  }
  Serial.printf("  Total: %d dispositivo(s)\n", n);
}

bool initMPU(Adafruit_MPU6050 &s, uint8_t addr, const char* name) {
  Serial.printf("[INIT] %s...", name);
  if (!s.begin(addr)) { Serial.println(" ❌"); return false; }
  s.setAccelerometerRange(MPU6050_RANGE_4_G);
  s.setGyroRange(MPU6050_RANGE_500_DEG);
  s.setFilterBandwidth(MPU6050_BAND_21_HZ);
  Serial.println(" ✅");
  return true;
}

bool initHX711() {
  Serial.print("[INIT] HX711...");
  scale.begin(HX711_DT, HX711_SCK);
  delay(500);
  if (scale.is_ready()) { Serial.println(" ✅"); scale.tare(); return true; }
  Serial.println(" ❌");
  return false;
}

bool initDS18B20() {
  Serial.print("[INIT] DS18B20...");
  ds18b20.begin();
  if (ds18b20.getDeviceCount() > 0) { Serial.println(" ✅"); return true; }
  Serial.println(" ❌");
  return false;
}

void leerMPU(Adafruit_MPU6050 &s, const char* name) {
  sensors_event_t a, g, t;
  s.getEvent(&a, &g, &t);
  Serial.printf("  [%s]\n", name);
  Serial.printf("    Accel: X=%+.2f Y=%+.2f Z=%+.2f m/s²\n", a.acceleration.x, a.acceleration.y, a.acceleration.z);
  Serial.printf("    Gyro:  X=%+.2f Y=%+.2f Z=%+.2f °/s\n", g.gyro.x, g.gyro.y, g.gyro.z);
  Serial.printf("    Temp:  %.1f°C\n", t.temperature);
}

void leerHX711() {
  Serial.println("  [HX711]");
  if (scale.is_ready()) Serial.printf("    Raw: %ld\n", scale.read());
  else Serial.println("    ERROR");
}

void leerDS18B20() {
  Serial.println("  [DS18B20]");
  ds18b20.requestTemperatures();
  float t = ds18b20.getTempCByIndex(0);
  if (t != DEVICE_DISCONNECTED_C) Serial.printf("    Temp: %.2f°C\n", t);
  else Serial.println("    ERROR");
}
