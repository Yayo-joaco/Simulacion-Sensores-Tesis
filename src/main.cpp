/*
 * PRUEBA FISICA - TODOS LOS SENSORES
 * Tesis: IoT-WSN y CCTV para monitoreo de subsistemas criticos
 *
 * Sensores soportados:
 * 1. MPU6050  (I2C 0x68) - Acelerometro/Giroscopio
 * 2. ADXL345  (I2C 0x53) - Acelerometro
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
 */

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_ADXL345_U.h>
#include <HX711.h>
#include <OneWire.h>
#include <DallasTemperature.h>

// === OBJETOS ===
Adafruit_MPU6050 mpu;
Adafruit_ADXL345_Unified adxl = Adafruit_ADXL345_Unified(12345);
HX711 scale;
OneWire oneWire(15);
DallasTemperature ds18b20(&oneWire);

// === ESTADO ===
bool mpuFound = false;
bool adxlFound = false;
bool hx711Ready = false;
bool ds18b20Ready = false;

// === PINES ===
#define HX711_DT  4
#define HX711_SCK 5
#define DS18B20_DQ 15
#define ANEMOMETER_PIN 34
#define VELETA_PIN 35

void setup() {
  Serial.begin(115200);
  delay(2000);

  Serial.println("================================================");
  Serial.println("  PRUEBA FISICA - TODOS LOS SENSORES");
  Serial.println("  Tesis IoT-WSN - Monitoreo Estructural");
  Serial.println("================================================");
  Serial.println();

  // --- I2C ---
  Wire.begin(21, 22);
  Serial.println("[I2C] Bus iniciado (SDA=21, SCL=22)");
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
    mpuFound = true;
    mpu.setAccelerometerRange(MPU6050_RANGE_4_G);
    mpu.setGyroRange(MPU6050_RANGE_500_DEG);
    mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
    Serial.println("[MPU6050] OK - Detectado en 0x68");
  } else if (mpu.begin(0x69)) {
    mpuFound = true;
    mpu.setAccelerometerRange(MPU6050_RANGE_4_G);
    mpu.setGyroRange(MPU6050_RANGE_500_DEG);
    mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
    Serial.println("[MPU6050] OK - Detectado en 0x69");
  } else {
    Serial.println("[MPU6050] NO detectado");
  }

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

  // --- HX711 ---
  Serial.println();
  Serial.println("[HX711] Inicializando (DT=4, SCK=5)...");
  scale.begin(HX711_DT, HX711_SCK);
  delay(500);
  if (scale.is_ready()) {
    hx711Ready = true;
    scale.set_scale(2280.f);  // Factor de calibracion (ajustar)
    scale.tare();
    Serial.println("[HX711] OK - Sensor detectado");
    Serial.println("[HX711] Tara realizada");
  } else {
    Serial.println("[HX711] NO detectado - Verifique conexion");
  }

  // --- DS18B20 ---
  Serial.println();
  Serial.println("[DS18B20] Inicializando (DQ=GPIO15)...");
  ds18b20.begin();
  int deviceCount = ds18b20.getDeviceCount();
  Serial.printf("[DS18B20] Dispositivos encontrados: %d\n", deviceCount);
  if (deviceCount > 0) {
    ds18b20Ready = true;
    Serial.println("[DS18B20] OK");
  } else {
    Serial.println("[DS18B20] NO detectado - Verifique conexion");
  }

  // --- ANEMOMETRO / VELETA ---
  Serial.println();
  Serial.println("[ANEMOMETRO] Pin analogico GPIO34");
  Serial.println("[VELETA] Pin analogico GPIO35");
  Serial.println("  (Lecturas analogicas - requieren sensor fisico)");

  Serial.println();
  Serial.println("================================================");
  Serial.println("  RESUMEN DE DETECCION:");
  Serial.printf("  MPU6050:   %s\n", mpuFound ? "OK" : "NO");
  Serial.printf("  ADXL345:   %s\n", adxlFound ? "OK" : "NO");
  Serial.printf("  HX711:     %s\n", hx711Ready ? "OK" : "NO");
  Serial.printf("  DS18B20:   %s\n", ds18b20Ready ? "OK" : "NO");
  Serial.printf("  Anemometro: %s\n", "ANALOG (GPIO34)");
  Serial.printf("  Veleta:     %s\n", "ANALOG (GPIO35)");
  Serial.println("================================================");
  Serial.println();
  Serial.println("--- Iniciando lecturas cada 2 segundos ---");
  Serial.println();
}

void loop() {
  static unsigned long lastRead = 0;
  if (millis() - lastRead < 2000) return;
  lastRead = millis();

  // --- MPU6050 ---
  if (mpuFound) {
    sensors_event_t a, g, temp;
    mpu.getEvent(&a, &g, &temp);
    Serial.println("[MPU6050]");
    Serial.printf("  Accel (m/s²): X=%.2f  Y=%.2f  Z=%.2f\n",
                  a.acceleration.x, a.acceleration.y, a.acceleration.z);
    Serial.printf("  Gyro (°/s):   X=%.2f  Y=%.2f  Z=%.2f\n",
                  g.gyro.x, g.gyro.y, g.gyro.z);
    Serial.printf("  Temp:         %.1f °C\n", temp.temperature);
  }

  // --- ADXL345 ---
  if (adxlFound) {
    sensors_event_t event;
    adxl.getEvent(&event);
    Serial.println("[ADXL345]");
    Serial.printf("  Accel (m/s²): X=%.2f  Y=%.2f  Z=%.2f\n",
                  event.acceleration.x, event.acceleration.y, event.acceleration.z);
  }

  // --- HX711 ---
  if (hx711Ready && scale.is_ready()) {
    float reading = scale.get_units(3);
    Serial.println("[HX711]");
    Serial.printf("  Peso: %.2f g\n", reading);
  }

  // --- DS18B20 ---
  if (ds18b20Ready) {
    ds18b20.requestTemperatures();
    float tempC = ds18b20.getTempCByIndex(0);
    Serial.println("[DS18B20]");
    Serial.printf("  Temperatura: %.2f °C\n", tempC);
  }

  // --- ANEMOMETRO / VELETA ---
  int anemVal = analogRead(ANEMOMETER_PIN);
  int veletaVal = analogRead(VELETA_PIN);
  Serial.println("[ANEMOMETRO/VELETA]");
  Serial.printf("  Anemometro (raw): %d\n", anemVal);
  Serial.printf("  Veleta (raw):     %d\n", veletaVal);

  Serial.println();
}
