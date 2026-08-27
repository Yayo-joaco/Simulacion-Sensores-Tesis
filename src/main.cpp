/*
 * ============================================================
 * TESIS: Sistema IoT-WSN y CCTV - Subsistema Estructural
 * Autores: Gerardo Rabanal y Gilmar Vargas
 * ============================================================
 * PRUEBA 8.2.5: DS18B20 (temperatura) + sensores previos
 * ------------------------------------------------------------
 * Objetivo: Validar la lectura del DS18B20 por protocolo
 *   1-Wire en el ESP32, junto con los sensores ya probados.
 *
 * Sensores conectados simultáneamente:
 *   1. MPU6050 #1 (I2C, 0x68) — acelerómetro/giroscopio
 *   2. MPU6050 #2 (I2C, 0x69) — simula ADXL345
 *   3. HX711 (DT/SCK) — galga extensométrica
 *   4. DS18B20 (1-Wire) — temperatura
 *
 * Conexiones DS18B20:
 *   DS18B20 VCC → ESP32 3V3
 *   DS18B20 GND → ESP32 GND
 *   DS18B20 DQ  → ESP32 GPIO 15
 *   (Pull-up 4.7kΩ entre DQ y VCC — incluido implícitamente en Wokwi)
 *
 * Limitaciones de la simulación:
 *   - Wokwi simula el protocolo 1-Wire correctamente
 *   - Se puede ajustar temperatura con slider (-55 a 125°C)
 *   - No simula: gradiente térmico real, compensación de galgas,
 *     dilatación térmica de la torre
 *   - Esta prueba valida: protocolo 1-Wire, lectura de temperatura,
 *     coexistencia con I2C y HX711 sin conflicto de pines
 * ============================================================
 */

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include "HX711.h"
#include <OneWire.h>
#include <DallasTemperature.h>

// --- Configuración de pines (según PDF Tabla 4) ---
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

// --- Contadores ---
unsigned long loopCount = 0;

// --- Prototipos ---
void imprimirBanner();
void imprimirSeparador();
void escanearBusI2C();
bool inicializarMPU(Adafruit_MPU6050 &sensor, uint8_t addr, const char* nombre);
bool inicializarHX711();
bool inicializarDS18B20();
void leerMPU(Adafruit_MPU6050 &sensor, const char* nombre);
void leerHX711();
void leerDS18B20();

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(1000);

  imprimirBanner();

  // Inicializar buses
  Wire.begin(I2C_SDA, I2C_SCL);
  Serial.println("[INIT] Bus I2C inicializado (GPIO 21/22)");
  Serial.println();

  // --- Escaneo I2C ---
  Serial.println("═══════════════════════════════════════════════");
  Serial.println("  Escaneo bus I2C");
  Serial.println("═══════════════════════════════════════════════");
  escanearBusI2C();
  Serial.println();

  // --- Inicializar sensores ---
  Serial.println("═══════════════════════════════════════════════");
  Serial.println("  Inicialización de sensores");
  Serial.println("═══════════════════════════════════════════════");

  bool s1_ok = inicializarMPU(sensor1, SENSOR1_ADDR, "MPU6050 #1 (0x68)");
  bool s2_ok = inicializarMPU(sensor2, SENSOR2_ADDR, "MPU6050 #2 (0x69)");
  bool hx_ok = inicializarHX711();
  bool ds_ok = inicializarDS18B20();
  Serial.println();

  // --- RESULTADO ---
  int sensoresOK = s1_ok + s2_ok + hx_ok + ds_ok;
  Serial.println("╔══════════════════════════════════════════════╗");
  Serial.println("║  RESULTADO: Sensores del subsistema          ║");
  Serial.println("╠══════════════════════════════════════════════╣");
  Serial.printf("║  MPU6050 #1 (I2C 0x68):  %s\n", s1_ok ? "✅ OK" : "❌ FAIL");
  Serial.printf("║  MPU6050 #2 (I2C 0x69):  %s\n", s2_ok ? "✅ OK" : "❌ FAIL");
  Serial.printf("║  HX711 (DT/SCK):         %s\n", hx_ok ? "✅ OK" : "❌ FAIL");
  Serial.printf("║  DS18B20 (1-Wire):       %s\n", ds_ok ? "✅ OK" : "❌ FAIL");
  Serial.println("╠══════════════════════════════════════════════╣");
  Serial.printf("║  Total: %d/4 sensores operativos             \n", sensoresOK);
  Serial.println("╚══════════════════════════════════════════════╝");
  Serial.println();

  imprimirSeparador();
}

// ============================================================
// LOOP - Lectura simultánea de todos los sensores
// ============================================================
void loop() {
  loopCount++;
  Serial.printf("═══ Ciclo #%lu ═══════════════════════════════\n", loopCount);

  leerMPU(sensor1, "MPU6050 #1 (0x68)");
  leerMPU(sensor2, "MPU6050 #2 (0x69)");
  leerHX711();
  leerDS18B20();

  Serial.println();
  delay(2000);
}

// ============================================================
// FUNCIONES
// ============================================================

void imprimirBanner() {
  Serial.println("╔══════════════════════════════════════════════╗");
  Serial.println("║  TESIS: IoT-WSN - Subsistema Estructural    ║");
  Serial.println("║  Prueba integrada: 4 sensores simultáneos   ║");
  Serial.println("║  MPU6050x2 + HX711 + DS18B20                ║");
  Serial.println("║  Autores: Rabanal / Vargas                   ║");
  Serial.println("║  Entorno: Wokwi + PlatformIO + ESP32         ║");
  Serial.println("╚══════════════════════════════════════════════╝");
  Serial.println();
}

void imprimirSeparador() {
  Serial.println("════════════════════════════════════════════════");
  Serial.println("  Lectura simultánea cada 2s");
  Serial.println("════════════════════════════════════════════════");
  Serial.println();
}

void escanearBusI2C() {
  int count = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  ✅ Dispositivo en 0x%02X", addr);
      if (addr == SENSOR1_ADDR) Serial.print("  ← MPU6050 #1");
      if (addr == SENSOR2_ADDR) Serial.print("  ← MPU6050 #2");
      Serial.println();
      count++;
    }
  }
  Serial.printf("  Total: %d dispositivo(s) I2C\n", count);
}

bool inicializarMPU(Adafruit_MPU6050 &sensor, uint8_t addr, const char* nombre) {
  Serial.printf("[INIT] %s...", nombre);
  if (!sensor.begin(addr)) {
    Serial.println(" ❌");
    return false;
  }
  sensor.setAccelerometerRange(MPU6050_RANGE_4_G);
  sensor.setGyroRange(MPU6050_RANGE_500_DEG);
  sensor.setFilterBandwidth(MPU6050_BAND_21_HZ);
  Serial.println(" ✅");
  return true;
}

bool inicializarHX711() {
  Serial.print("[INIT] HX711 (GPIO 4/5)...");
  scale.begin(HX711_DT, HX711_SCK);
  delay(500);
  if (scale.is_ready()) {
    Serial.println(" ✅");
    scale.tare();
    return true;
  }
  Serial.println(" ❌");
  return false;
}

bool inicializarDS18B20() {
  Serial.print("[INIT] DS18B20 (GPIO 15)...");
  ds18b20.begin();

  int deviceCount = ds18b20.getDeviceCount();
  if (deviceCount > 0) {
    Serial.printf(" ✅ (%d sensor(es))\n", deviceCount);
    return true;
  }
  Serial.println(" ❌");
  return false;
}

void leerMPU(Adafruit_MPU6050 &sensor, const char* nombre) {
  sensors_event_t accel, gyro, temp;
  sensor.getEvent(&accel, &gyro, &temp);

  Serial.printf("  [%s]\n", nombre);
  Serial.printf("    Accel: X=%+.2f Y=%+.2f Z=%+.2f m/s²\n",
                accel.acceleration.x, accel.acceleration.y, accel.acceleration.z);
  Serial.printf("    Gyro:  X=%+.2f Y=%+.2f Z=%+.2f °/s\n",
                gyro.gyro.x, gyro.gyro.y, gyro.gyro.z);
  Serial.printf("    Temp:  %.1f°C\n", temp.temperature);
}

void leerHX711() {
  Serial.println("  [HX711]");
  if (scale.is_ready()) {
    long raw = scale.read();
    Serial.printf("    Raw: %ld\n", raw);
  } else {
    Serial.println("    ERROR: No disponible");
  }
}

void leerDS18B20() {
  Serial.println("  [DS18B20]");
  ds18b20.requestTemperatures();

  float tempC = ds18b20.getTempCByIndex(0);
  if (tempC != DEVICE_DISCONNECTED_C) {
    Serial.printf("    Temperatura: %.2f°C\n", tempC);
  } else {
    Serial.println("    ERROR: Sensor desconectado");
  }
}
