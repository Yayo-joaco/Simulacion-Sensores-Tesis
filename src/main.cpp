/*
 * ============================================================
 * TESIS: IoT-WSN - Subsistema Estructural
 * Autores: Gerardo Rabanal / Gilmar Vargas
 * ============================================================
 * PRUEBA INTEGRADA: 4 sensores simultáneos
 *   MPU6050 (0x68) + ADXL345 (0x53) + HX711 + DS18B20
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

// --- Objetos ---
Adafruit_MPU6050 mpu;
HX711 scale;
OneWire oneWire(DS18B20_DQ);
DallasTemperature ds18b20(&oneWire);

unsigned long loopCount = 0;

// --- Prototipos ---
void imprimirBanner();
void escanearBusI2C();
bool initMPU6050();
bool initADXL345();
bool initHX711();
bool initDS18B20();
void leerMPU6050();
void leerADXL345();
void leerHX711();
void leerDS18B20();

// ============================================================
void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(1000);
  imprimirBanner();

  Wire.begin(I2C_SDA, I2C_SCL);
  Serial.println("[INIT] Bus I2C (GPIO 21/22)\n");

  Serial.println("═══════════════════════════════════════");
  Serial.println("  Escaneo I2C");
  Serial.println("═══════════════════════════════════════");
  escanearBusI2C();
  Serial.println();

  Serial.println("═══════════════════════════════════════");
  Serial.println("  Inicialización");
  Serial.println("═══════════════════════════════════════");
  bool s1 = initMPU6050();
  bool s2 = initADXL345();
  bool hx = initHX711();
  bool ds = initDS18B20();
  Serial.println();

  int ok = s1 + s2 + hx + ds;
  Serial.println("╔═══════════════════════════════════════╗");
  Serial.println("║  RESULTADO: Subsistema Estructural    ║");
  Serial.println("╠═══════════════════════════════════════╣");
  Serial.printf("║  MPU6050 (0x68):  %s\n", s1?"✅ OK":"❌ FAIL");
  Serial.printf("║  ADXL345 (0x53):  %s\n", s2?"✅ OK":"❌ FAIL");
  Serial.printf("║  HX711 (DT/SCK):  %s\n", hx?"✅ OK":"❌ FAIL");
  Serial.printf("║  DS18B20 (1-Wire):%s\n", ds?"✅ OK":"❌ FAIL");
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
  leerMPU6050();
  leerADXL345();
  leerHX711();
  leerDS18B20();
  Serial.println();
  delay(2000);
}

// ============================================================
void imprimirBanner() {
  Serial.println("╔═══════════════════════════════════════╗");
  Serial.println("║  TESIS: IoT-WSN - Subsistema Estruc. ║");
  Serial.println("║  MPU6050+ADXL345+HX711+DS18B20       ║");
  Serial.println("║  Autores: Rabanal / Vargas            ║");
  Serial.println("╚═══════════════════════════════════════╝\n");
}

void escanearBusI2C() {
  int n = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  ✅ 0x%02X", a);
      if (a == MPU6050_ADDR) Serial.print(" ← MPU6050");
      if (a == ADXL345_ADDR) Serial.print(" ← ADXL345");
      Serial.println();
      n++;
    }
  }
  Serial.printf("  Total: %d dispositivo(s)\n", n);
}

bool initMPU6050() {
  Serial.print("[INIT] MPU6050 (0x68)...");
  if (!mpu.begin()) { Serial.println(" ❌"); return false; }
  mpu.setAccelerometerRange(MPU6050_RANGE_4_G);
  mpu.setGyroRange(MPU6050_RANGE_500_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
  Serial.println(" ✅");
  return true;
}

bool initADXL345() {
  Serial.print("[INIT] ADXL345 (0x53)...");
  Wire.beginTransmission(ADXL345_ADDR);
  Wire.write(ADXL345_REG_DEVID);
  Wire.endTransmission(false);
  Wire.requestFrom((int)ADXL345_ADDR, (int)1);
  if (Wire.available()) {
    uint8_t id = Wire.read();
    if (id == 0xE5) {
      Wire.beginTransmission(ADXL345_ADDR);
      Wire.write(ADXL345_REG_POWER_CTL);
      Wire.write(0x08);
      Wire.endTransmission();
      Serial.printf(" ✅ (ID=0x%02X)\n", id);
      return true;
    }
    Serial.printf(" ❌ (ID=0x%02X)\n", id);
    return false;
  }
  Serial.println(" ❌");
  return false;
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

void leerMPU6050() {
  sensors_event_t a, g, t;
  mpu.getEvent(&a, &g, &t);
  Serial.println("  [MPU6050 @ 0x68]");
  Serial.printf("    Accel: X=%+.2f Y=%+.2f Z=%+.2f m/s²\n", a.acceleration.x, a.acceleration.y, a.acceleration.z);
  Serial.printf("    Gyro:  X=%+.2f Y=%+.2f Z=%+.2f °/s\n", g.gyro.x, g.gyro.y, g.gyro.z);
  Serial.printf("    Temp:  %.1f°C\n", t.temperature);
}

void leerADXL345() {
  Serial.println("  [ADXL345 @ 0x53]");
  Wire.beginTransmission(ADXL345_ADDR);
  Wire.write(ADXL345_REG_DATAX0);
  Wire.endTransmission(false);
  Wire.requestFrom((int)ADXL345_ADDR, (int)6);
  if (Wire.available() >= 6) {
    uint8_t x0=Wire.read(), x1=Wire.read();
    uint8_t y0=Wire.read(), y1=Wire.read();
    uint8_t z0=Wire.read(), z1=Wire.read();
    int16_t rx=(int16_t)(x0|(x1<<8));
    int16_t ry=(int16_t)(y0|(y1<<8));
    int16_t rz=(int16_t)(z0|(z1<<8));
    Serial.printf("    Raw:   X=%+6d Y=%+6d Z=%+6d\n", rx, ry, rz);
    Serial.printf("    Accel: X=%+.2f Y=%+.2f Z=%+.2f m/s²\n",
      (rx/256.0)*9.80665, (ry/256.0)*9.80665, (rz/256.0)*9.80665);
  } else {
    Serial.println("    ERROR");
  }
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
