/*
 * ============================================================
 * TESIS: Sistema IoT-WSN y CCTV - Subsistema Estructural
 * Autores: Gerardo Rabanal y Gilmar Vargas
 * ============================================================
 * PRUEBA 8.2.1: Bus I2C compartido (MPU6050 + ADXL345)
 * ------------------------------------------------------------
 * Objetivo: Validar que ambos sensores responden con direcciones
 *   I2C distintas (0x68 y 0x53) sin conflicto en el mismo bus
 *   del ESP32.
 *
 * Entorno: Wokwi (simulador) + PlatformIO + VS Code
 *
 * Conexiones (comparten el mismo bus I2C):
 *   MPU6050 VCC → ESP32 3V3    ADXL345 VCC → ESP32 3V3
 *   MPU6050 GND → ESP32 GND    ADXL345 GND → ESP32 GND
 *   MPU6050 SCL → ESP32 GPIO22 ADXL345 SCL → ESP32 GPIO22
 *   MPU6050 SDA → ESP32 GPIO21 ADXL345 SDA → ESP32 GPIO21
 *
 * Limitaciones de la simulación:
 *   - El ADXL345 es un custom chip de Wokwi que simula solo
 *     el protocolo I2C (dirección 0x53, registros de datos)
 *   - No simula vibración física real ni movimiento
 *   - Esta prueba valida ÚNICAMENTE: coexistencia de dos
 *     dispositivos I2C en el mismo bus sin conflictos
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
#define MPU6050_ADDR  0x68
#define ADXL345_ADDR  0x53

// --- Registros del ADXL345 (para lectura directa) ---
#define ADXL345_REG_DEVID     0x00
#define ADXL345_REG_DATAX0    0x32
#define ADXL345_REG_POWER_CTL 0x2D

// --- Objeto MPU6050 (librería Adafruit) ---
Adafruit_MPU6050 mpu;

// --- Contadores ---
unsigned long loopCount = 0;

// --- Prototipos ---
void escanearBusI2C();
bool verificarDispositivo(uint8_t addr, const char* nombre);
bool inicializarMPU6050();
void inicializarADXL345();
void leerMPU6050();
void leerADXL345();
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
  bool mpu_ok = verificarDispositivo(MPU6050_ADDR, "MPU6050");
  bool adxl_ok = verificarDispositivo(ADXL345_ADDR, "ADXL345");
  Serial.println();

  // --- FASE 3: Inicialización de sensores ---
  Serial.println("═══════════════════════════════════════════════");
  Serial.println("  FASE 3: Inicialización de sensores");
  Serial.println("═══════════════════════════════════════════════");
  if (mpu_ok) {
    inicializarMPU6050();
  } else {
    Serial.println("[ERROR] MPU6050 no detectado. Omitiendo inicialización.");
  }
  if (adxl_ok) {
    inicializarADXL345();
  } else {
    Serial.println("[ERROR] ADXL345 no detectado. Omitiendo inicialización.");
  }
  Serial.println();

  // --- RESULTADO DE LA PRUEBA ---
  Serial.println("╔══════════════════════════════════════════════╗");
  Serial.println("║  RESULTADO PRUEBA 8.2.1: Bus I2C compartido ║");
  Serial.println("╠══════════════════════════════════════════════╣");
  if (mpu_ok && adxl_ok) {
    Serial.println("║  ✅ EXITOSA: Ambos sensores detectados      ║");
    Serial.println("║  MPU6050 (0x68) y ADXL345 (0x53) coexisten  ║");
    Serial.println("║  sin conflicto en el mismo bus I2C           ║");
  } else if (mpu_ok || adxl_ok) {
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

  // Leer MPU6050
  Serial.println("  [MPU6050 @ 0x68]");
  leerMPU6050();

  // Leer ADXL345
  Serial.println("  [ADXL345 @ 0x53]");
  leerADXL345();

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
      if (addr == MPU6050_ADDR) Serial.print("  ← MPU6050");
      if (addr == ADXL345_ADDR) Serial.print("  ← ADXL345");
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

bool inicializarMPU6050() {
  Serial.println("[INIT] Inicializando MPU6050...");

  if (!mpu.begin()) {
    Serial.println("[INIT]   ERROR: No se pudo inicializar");
    return false;
  }

  mpu.setAccelerometerRange(MPU6050_RANGE_4_G);
  mpu.setGyroRange(MPU6050_RANGE_500_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);

  Serial.println("[INIT]   MPU6050 inicializado correctamente");
  Serial.println("[INIT]   Rango: ±4g / ±500°/s / Filtro 21Hz");
  return true;
}

void inicializarADXL345() {
  Serial.println("[INIT] Inicializando ADXL345...");

  // Leer Device ID directamente por I2C
  Wire.beginTransmission(ADXL345_ADDR);
  Wire.write(ADXL345_REG_DEVID);
  Wire.endTransmission(false);
  Wire.requestFrom((int)ADXL345_ADDR, (int)1);
  uint8_t devId = Wire.read();

  Serial.printf("[INIT]   Device ID: 0x%02X", devId);
  if (devId == 0xE5) {
    Serial.println(" (correcto para ADXL345)");
  } else {
    Serial.println(" (inesperado)");
  }

  // Activar modo medición (set bit 3 en POWER_CTL)
  Wire.beginTransmission(ADXL345_ADDR);
  Wire.write(ADXL345_REG_POWER_CTL);
  Wire.write(0x08);  // Measure bit
  Wire.endTransmission();

  Serial.println("[INIT]   ADXL345 en modo medición");
}

void leerMPU6050() {
  sensors_event_t accel, gyro, temp;
  mpu.getEvent(&accel, &gyro, &temp);

  Serial.printf("    Accel: X=%+.3f Y=%+.3f Z=%+.3f m/s²\n",
                accel.acceleration.x, accel.acceleration.y, accel.acceleration.z);
  Serial.printf("    Gyro:  X=%+.3f Y=%+.3f Z=%+.3f °/s\n",
                gyro.gyro.x, gyro.gyro.y, gyro.gyro.z);
  Serial.printf("    Temp:  %.1f°C\n", temp.temperature);
}

void leerADXL345() {
  // Leer 6 bytes de datos (X0, X1, Y0, Y1, Z0, Z1)
  Wire.beginTransmission(ADXL345_ADDR);
  Wire.write(ADXL345_REG_DATAX0);
  Wire.endTransmission(false);
  Wire.requestFrom((int)ADXL345_ADDR, (int)6);

  if (Wire.available() >= 6) {
    uint8_t x0 = Wire.read(); uint8_t x1 = Wire.read();
    uint8_t y0 = Wire.read(); uint8_t y1 = Wire.read();
    uint8_t z0 = Wire.read(); uint8_t z1 = Wire.read();

    // Reconstruir valores 16-bit (little-endian, dos complemento)
    int16_t raw_x = (int16_t)(x0 | (x1 << 8));
    int16_t raw_y = (int16_t)(y0 | (y1 << 8));
    int16_t raw_z = (int16_t)(z0 | (z1 << 8));

    // Convertir a g (sensibilidad: 256 LSB/g en modo ±2g)
    float gx = raw_x / 256.0;
    float gy = raw_y / 256.0;
    float gz = raw_z / 256.0;

    // Convertir a m/s² (1g = 9.80665 m/s²)
    float ax = gx * 9.80665;
    float ay = gy * 9.80665;
    float az = gz * 9.80665;

    Serial.printf("    Raw:   X=%+6d Y=%+6d Z=%+6d\n", raw_x, raw_y, raw_z);
    Serial.printf("    Accel: X=%+.3f Y=%+.3f Z=%+.3f m/s²\n", ax, ay, az);
  } else {
    Serial.println("    ERROR: No se pudieron leer datos del ADXL345");
  }
}

void imprimirBanner() {
  Serial.println("╔══════════════════════════════════════════════╗");
  Serial.println("║  TESIS: IoT-WSN - Subsistema Estructural    ║");
  Serial.println("║  Prueba 8.2.1: Bus I2C compartido           ║");
  Serial.println("║  MPU6050 (0x68) + ADXL345 (0x53)            ║");
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
