/*
 * ============================================================
 * TESIS: Sistema IoT-WSN y CCTV - Subsistema Estructural
 * Autores: Gerardo Rabanal y Gilmar Vargas
 * ============================================================
 * PRUEBA INTEGRADA: 4 sensores simultáneos
 * ------------------------------------------------------------
 * Sensores conectados:
 *   1. MPU6050 (I2C, 0x68) — acelerómetro/giroscopio
 *   2. ADXL345 (I2C, 0x53) — acelerómetro (custom chip Wokwi)
 *   3. HX711 (DT/SCK) — galga extensométrica
 *   4. DS18B20 (1-Wire) — temperatura
 *
 * Protocolos validados:
 *   - I2C compartido (MPU6050 + ADXL345 en mismo bus)
 *   - Protocolo dedicado 2 hilos (HX711)
 *   - 1-Wire (DS18B20)
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
#define MPU6050_ADDR  0x68
#define ADXL345_ADDR  0x53

// --- Registros ADXL345 ---
#define ADXL345_REG_DEVID     0x00
#define ADXL345_REG_POWER_CTL 0x2D
#define ADXL345_REG_DATAX0    0x32

// --- Objetos sensores ---
Adafruit_MPU6050 mpu;
HX711 scale;
OneWire oneWire(DS18B20_DQ);
DallasTemperature ds18b20(&oneWire);

// --- Contadores ---
unsigned long loopCount = 0;

// --- Prototipos ---
void imprimirBanner();
void escanearBusI2C();
bool inicializarMPU6050();
bool inicializarADXL345();
bool inicializarHX711();
bool inicializarDS18B20();
void leerMPU6050();
void leerADXL345();
void leerHX711();
void leerDS18B20();

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(1000);

  imprimirBanner();

  Wire.begin(I2C_SDA, I2C_SCL);
  Serial.println("[INIT] Bus I2C inicializado (GPIO 21/22)");
  Serial.println();

  // Escaneo I2C
  Serial.println("═══════════════════════════════════════════════");
  Serial.println("  Escaneo bus I2C");
  Serial.println("═══════════════════════════════════════════════");
  escanearBusI2C();
  Serial.println();

  // Inicializar sensores
  Serial.println("═══════════════════════════════════════════════");
  Serial.println("  Inicialización de sensores");
  Serial.println("═══════════════════════════════════════════════");

  bool mpu_ok = inicializarMPU6050();
  bool adxl_ok = inicializarADXL345();
  bool hx_ok = inicializarHX711();
  bool ds_ok = inicializarDS18B20();
  Serial.println();

  // Resultado
  int total = mpu_ok + adxl_ok + hx_ok + ds_ok;
  Serial.println("╔══════════════════════════════════════════════╗");
  Serial.println("║  RESULTADO: Subsistema Estructural           ║");
  Serial.println("╠══════════════════════════════════════════════╣");
  Serial.printf("║  MPU6050 (I2C 0x68):   %s\n", mpu_ok ? "✅ OK" : "❌ FAIL");
  Serial.printf("║  ADXL345 (I2C 0x53):   %s\n", adxl_ok ? "✅ OK" : "❌ FAIL");
  Serial.printf("║  HX711 (DT/SCK):       %s\n", hx_ok ? "✅ OK" : "❌ FAIL");
  Serial.printf("║  DS18B20 (1-Wire):     %s\n", ds_ok ? "✅ OK" : "❌ FAIL");
  Serial.println("╠══════════════════════════════════════════════╣");
  Serial.printf("║  Total: %d/4 sensores operativos             \n", total);
  Serial.println("╚══════════════════════════════════════════════╝");
  Serial.println();

  Serial.println("════════════════════════════════════════════════");
  Serial.println("  Lectura simultánea cada 2s");
  Serial.println("════════════════════════════════════════════════");
  Serial.println();
}

// ============================================================
// LOOP
// ============================================================
void loop() {
  loopCount++;
  Serial.printf("═══ Ciclo #%lu ═══════════════════════════════\n", loopCount);

  leerMPU6050();
  leerADXL345();
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
  Serial.println("║  MPU6050 + ADXL345 + HX711 + DS18B20        ║");
  Serial.println("║  Autores: Rabanal / Vargas                   ║");
  Serial.println("║  Entorno: Wokwi + PlatformIO + ESP32         ║");
  Serial.println("╚══════════════════════════════════════════════╝");
  Serial.println();
}

void escanearBusI2C() {
  int count = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  ✅ Dispositivo en 0x%02X", addr);
      if (addr == MPU6050_ADDR) Serial.print("  ← MPU6050");
      if (addr == ADXL345_ADDR) Serial.print("  ← ADXL345");
      Serial.println();
      count++;
    }
  }
  Serial.printf("  Total: %d dispositivo(s) I2C\n", count);
}

bool inicializarMPU6050() {
  Serial.print("[INIT] MPU6050 (0x68)...");
  if (!mpu.begin()) {
    Serial.println(" ❌");
    return false;
  }
  mpu.setAccelerometerRange(MPU6050_RANGE_4_G);
  mpu.setGyroRange(MPU6050_RANGE_500_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
  Serial.println(" ✅");
  return true;
}

bool inicializarADXL345() {
  Serial.print("[INIT] ADXL345 (0x53)...");

  // Verificar Device ID
  Wire.beginTransmission(ADXL345_ADDR);
  Wire.write(ADXL345_REG_DEVID);
  Wire.endTransmission(false);
  Wire.requestFrom((int)ADXL345_ADDR, (int)1);

  if (Wire.available()) {
    uint8_t devId = Wire.read();
    if (devId == 0xE5) {
      // Activar modo medición
      Wire.beginTransmission(ADXL345_ADDR);
      Wire.write(ADXL345_REG_POWER_CTL);
      Wire.write(0x08);
      Wire.endTransmission();
      Serial.printf(" ✅ (ID=0x%02X)\n", devId);
      return true;
    }
    Serial.printf(" ❌ (ID inesperado: 0x%02X)\n", devId);
    return false;
  }
  Serial.println(" ❌");
  return false;
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
  int n = ds18b20.getDeviceCount();
  if (n > 0) {
    Serial.printf(" ✅ (%d sensor)\n", n);
    return true;
  }
  Serial.println(" ❌");
  return false;
}

void leerMPU6050() {
  sensors_event_t accel, gyro, temp;
  mpu.getEvent(&accel, &gyro, &temp);

  Serial.println("  [MPU6050 @ 0x68]");
  Serial.printf("    Accel: X=%+.2f Y=%+.2f Z=%+.2f m/s²\n",
                accel.acceleration.x, accel.acceleration.y, accel.acceleration.z);
  Serial.printf("    Gyro:  X=%+.2f Y=%+.2f Z=%+.2f °/s\n",
                gyro.gyro.x, gyro.gyro.y, gyro.gyro.z);
  Serial.printf("    Temp:  %.1f°C\n", temp.temperature);
}

void leerADXL345() {
  Serial.println("  [ADXL345 @ 0x53]");

  // Leer 6 bytes desde registro DATAX0
  Wire.beginTransmission(ADXL345_ADDR);
  Wire.write(ADXL345_REG_DATAX0);
  Wire.endTransmission(false);
  Wire.requestFrom((int)ADXL345_ADDR, (int)6);

  if (Wire.available() >= 6) {
    uint8_t x0 = Wire.read(); uint8_t x1 = Wire.read();
    uint8_t y0 = Wire.read(); uint8_t y1 = Wire.read();
    uint8_t z0 = Wire.read(); uint8_t z1 = Wire.read();

    int16_t raw_x = (int16_t)(x0 | (x1 << 8));
    int16_t raw_y = (int16_t)(y0 | (y1 << 8));
    int16_t raw_z = (int16_t)(z0 | (z1 << 8));

    // Convertir a m/s² (256 LSB/g, 1g = 9.80665 m/s²)
    float ax = (raw_x / 256.0) * 9.80665;
    float ay = (raw_y / 256.0) * 9.80665;
    float az = (raw_z / 256.0) * 9.80665;

    Serial.printf("    Raw:   X=%+6d Y=%+6d Z=%+6d\n", raw_x, raw_y, raw_z);
    Serial.printf("    Accel: X=%+.2f Y=%+.2f Z=%+.2f m/s²\n", ax, ay, az);
  } else {
    Serial.println("    ERROR: No se pudo leer");
  }
}

void leerHX711() {
  Serial.println("  [HX711]");
  if (scale.is_ready()) {
    Serial.printf("    Raw: %ld\n", scale.read());
  } else {
    Serial.println("    ERROR: No disponible");
  }
}

void leerDS18B20() {
  Serial.println("  [DS18B20]");
  ds18b20.requestTemperatures();
  float t = ds18b20.getTempCByIndex(0);
  if (t != DEVICE_DISCONNECTED_C) {
    Serial.printf("    Temp: %.2f°C\n", t);
  } else {
    Serial.println("    ERROR: Desconectado");
  }
}
