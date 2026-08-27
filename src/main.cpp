/*
 * ============================================================
 * TESIS: Sistema IoT-WSN y CCTV - Subsistema Estructural
 * Autores: Gerardo Rabanal y Gilmar Vargas
 * ============================================================
 * PRUEBA 8.2.1: Bus I2C compartido (dos dispositivos)
 * ------------------------------------------------------------
 * Objetivo: Validar que dos sensores I2C responden con
 *   direcciones distintas sin conflicto en el mismo bus.
 *
 * Método: Dos MPU6050 nativos de Wokwi:
 *   - MPU1: AD0=GND → dirección 0x68 (simula MPU6050 real)
 *   - MPU2: AD0=VCC → dirección 0x69 (simula rol del ADXL345)
 *
 * Limitaciones de la simulación:
 *   - Wokwi no tiene ADXL345 nativo ni soporta custom chips
 *     en la extensión VS Code sin compilación manual a WASM
 *   - Se usa MPU6050 con dirección alternativa para demostrar
 *     el concepto de coexistencia I2C (dos direcciones distintas)
 *   - El concepto validado es idéntico: el bus I2C del ESP32
 *     maneja múltiples dispositivos sin conflicto
 *   - La prueba real con ADXL345 (0x53) se validará con
 *     hardware físico en la fase de laboratorio
 * ============================================================
 */

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>

// --- Configuración I2C ---
#define I2C_SDA 21
#define I2C_SCL 22
#define SERIAL_BAUD 115200

// --- Direcciones I2C ---
#define SENSOR1_ADDR  0x68  // MPU6050 con AD0=GND
#define SENSOR2_ADDR  0x69  // MPU6050 con AD0=VCC (simula ADXL345)

// --- Objetos sensores ---
Adafruit_MPU6050 sensor1;
Adafruit_MPU6050 sensor2;

// --- Contadores ---
unsigned long loopCount = 0;

// --- Prototipos ---
void escanearBusI2C();
bool verificarDispositivo(uint8_t addr, const char* nombre);
bool inicializarSensor(Adafruit_MPU6050 &sensor, uint8_t addr, const char* nombre);
void leerSensor(Adafruit_MPU6050 &sensor, const char* nombre);
void imprimirBanner();
void imprimirSeparador();

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(1000);

  imprimirBanner();

  // Inicializar bus I2C
  Wire.begin(I2C_SDA, I2C_SCL);
  Serial.println("[INIT] Bus I2C inicializado");
  Serial.printf("[INIT]   SDA: GPIO %d\n", I2C_SDA);
  Serial.printf("[INIT]   SCL: GPIO %d\n", I2C_SCL);
  Serial.println();

  // --- FASE 1: Escaneo del bus I2C ---
  Serial.println("═══════════════════════════════════════════════");
  Serial.println("  FASE 1: Escaneo del bus I2C");
  Serial.println("═══════════════════════════════════════════════");
  escanearBusI2C();
  Serial.println();

  // --- FASE 2: Verificación de direcciones ---
  Serial.println("═══════════════════════════════════════════════");
  Serial.println("  FASE 2: Verificación de direcciones I2C");
  Serial.println("═══════════════════════════════════════════════");
  bool s1_ok = verificarDispositivo(SENSOR1_ADDR, "Sensor1 (MPU6050)");
  bool s2_ok = verificarDispositivo(SENSOR2_ADDR, "Sensor2 (simula ADXL345)");
  Serial.println();

  // --- FASE 3: Inicialización de sensores ---
  Serial.println("═══════════════════════════════════════════════");
  Serial.println("  FASE 3: Inicialización de sensores");
  Serial.println("═══════════════════════════════════════════════");
  if (s1_ok) {
    inicializarSensor(sensor1, SENSOR1_ADDR, "Sensor1 (0x68)");
  } else {
    Serial.println("[ERROR] Sensor1 no detectado.");
  }
  if (s2_ok) {
    inicializarSensor(sensor2, SENSOR2_ADDR, "Sensor2 (0x69)");
  } else {
    Serial.println("[ERROR] Sensor2 no detectado.");
  }
  Serial.println();

  // --- RESULTADO DE LA PRUEBA ---
  Serial.println("╔══════════════════════════════════════════════╗");
  Serial.println("║  RESULTADO PRUEBA 8.2.1: Bus I2C compartido ║");
  Serial.println("╠══════════════════════════════════════════════╣");
  if (s1_ok && s2_ok) {
    Serial.println("║  ✅ EXITOSA: Dos dispositivos I2C detectados ║");
    Serial.println("║  Sensor1 (0x68) y Sensor2 (0x69) coexisten   ║");
    Serial.println("║  sin conflicto en el mismo bus I2C           ║");
  } else if (s1_ok || s2_ok) {
    Serial.println("║  ⚠️  PARCIAL: Solo un sensor detectado       ║");
  } else {
    Serial.println("║  ❌ FALLIDA: Ningún sensor detectado         ║");
  }
  Serial.println("╚══════════════════════════════════════════════╝");
  Serial.println();

  imprimirSeparador();
}

// ============================================================
// LOOP - Lectura simultánea de ambos sensores
// ============================================================
void loop() {
  loopCount++;
  Serial.printf("─── Ciclo #%lu ───────────────────────────────\n", loopCount);

  Serial.println("  [Sensor1 @ 0x68 - MPU6050]");
  leerSensor(sensor1, "Sensor1");

  Serial.println("  [Sensor2 @ 0x69 - simula ADXL345]");
  leerSensor(sensor2, "Sensor2");

  Serial.println();
  delay(2000);
}

// ============================================================
// FUNCIONES
// ============================================================

void escanearBusI2C() {
  int dispositivosEncontrados = 0;

  Serial.println("[SCAN] Escaneando direcciones 0x01 - 0x7F...");
  Serial.println();

  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    uint8_t error = Wire.endTransmission();

    if (error == 0) {
      Serial.printf("  ✅ Dispositivo encontrado en 0x%02X", addr);
      if (addr == SENSOR1_ADDR) Serial.print("  ← Sensor1 (MPU6050)");
      if (addr == SENSOR2_ADDR) Serial.print("  ← Sensor2 (simula ADXL345)");
      Serial.println();
      dispositivosEncontrados++;
    }
  }

  Serial.println();
  Serial.printf("[SCAN] Total: %d dispositivo(s) encontrado(s)\n", dispositivosEncontrados);
}

bool verificarDispositivo(uint8_t addr, const char* nombre) {
  Wire.beginTransmission(addr);
  uint8_t error = Wire.endTransmission();

  if (error == 0) {
    Serial.printf("[OK]   %s responde en dirección 0x%02X\n", nombre, addr);
    return true;
  } else {
    Serial.printf("[FAIL] %s NO responde en dirección 0x%02X (error=%d)\n",
                  nombre, addr, error);
    return false;
  }
}

bool inicializarSensor(Adafruit_MPU6050 &sensor, uint8_t addr, const char* nombre) {
  Serial.printf("[INIT] Inicializando %s...\n", nombre);

  if (!sensor.begin(addr)) {
    Serial.printf("[INIT]   ERROR: No se pudo inicializar %s\n", nombre);
    return false;
  }

  sensor.setAccelerometerRange(MPU6050_RANGE_4_G);
  sensor.setGyroRange(MPU6050_RANGE_500_DEG);
  sensor.setFilterBandwidth(MPU6050_BAND_21_HZ);

  Serial.printf("[INIT]   %s inicializado correctamente\n", nombre);
  Serial.println("[INIT]   Rango: ±4g / ±500°/s / Filtro 21Hz");
  return true;
}

void leerSensor(Adafruit_MPU6050 &sensor, const char* nombre) {
  sensors_event_t accel, gyro, temp;
  sensor.getEvent(&accel, &gyro, &temp);

  Serial.printf("    Accel: X=%+.3f Y=%+.3f Z=%+.3f m/s²\n",
                accel.acceleration.x, accel.acceleration.y, accel.acceleration.z);
  Serial.printf("    Gyro:  X=%+.3f Y=%+.3f Z=%+.3f °/s\n",
                gyro.gyro.x, gyro.gyro.y, gyro.gyro.z);
  Serial.printf("    Temp:  %.1f°C\n", temp.temperature);
}

void imprimirBanner() {
  Serial.println("╔══════════════════════════════════════════════╗");
  Serial.println("║  TESIS: IoT-WSN - Subsistema Estructural    ║");
  Serial.println("║  Prueba 8.2.1: Bus I2C compartido           ║");
  Serial.println("║  Dos dispositivos I2C en el mismo bus        ║");
  Serial.println("║  Autores: Rabanal / Vargas                   ║");
  Serial.println("║  Entorno: Wokwi + PlatformIO + ESP32         ║");
  Serial.println("╚══════════════════════════════════════════════╝");
  Serial.println();
}

void imprimirSeparador() {
  Serial.println("════════════════════════════════════════════════");
  Serial.println("  Lectura simultánea cada 2s (Ctrl+C para fin)");
  Serial.println("════════════════════════════════════════════════");
  Serial.println();
}
