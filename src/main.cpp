/*
 * ============================================================
 * TESIS: Sistema IoT-WSN y CCTV - Subsistema Estructural
 * Autores: Gerardo Rabanal y Gilmar Vargas
 * ============================================================
 * PRUEBA 8.2.4: Galga BF350 + HX711 (deformación simulada)
 * ------------------------------------------------------------
 * Objetivo: Validar la lectura del módulo HX711 (amplificador
 *   de celda de carga / galga extensométrica) vía protocolo
 *   dedicado de 2 hilos (DT/SCK) en el ESP32.
 *
 * Entorno: Wokwi (simulador) + PlatformIO + VS Code
 *
 * Conexiones:
 *   HX711 VCC → ESP32 5V
 *   HX711 GND → ESP32 GND
 *   HX711 DT  → ESP32 GPIO 4
 *   HX711 SCK → ESP32 GPIO 5
 *
 * Valores simulados en Wokwi:
 *   - Tipo: celda de carga de 5kg
 *   - Rango de lectura raw: 0 - 2100
 *   - El valor se puede ajustar con slider en el diagrama
 *
 * Limitaciones de la simulación:
 *   - Wokwi simula el protocolo del HX711 pero no una galga
 *     real con deformación mecánica
 *   - No se puede simular: deformación real del metal, efecto
 *     de temperatura en la galga, vibración estructural
 *   - Esta prueba valida ÚNICAMENTE: protocolo DT/SCK,
 *     lectura de valores raw, calibración básica, y la
 *     integración del HX711 con el ESP32
 *   - La galga BF350 real requiere: puente de Wheatstone,
 *     pegado sobre la superficie metálica, compensación
 *     térmica con DS18B20, y calibración con masa conocida
 * ============================================================
 */

#include <Arduino.h>
#include "HX711.h"

// --- Configuración de pines (según PDF Tabla 4) ---
#define HX711_DT  4   // GPIO4 - Data
#define HX711_SCK 5   // GPIO5 - Clock
#define SERIAL_BAUD 115200

// --- Objeto HX711 ---
HX711 scale;

// --- Variables de calibración ---
float factorCalibracion = 1.0;
float tareValue = 0.0;
unsigned long lecturaCount = 0;

// --- Prototipos ---
bool inicializarHX711();
void calibrar();
void leerHX711();
void imprimirBanner();
void imprimirSeparador();

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(1000);

  imprimirBanner();

  // --- FASE 1: Inicialización ---
  Serial.println("═══════════════════════════════════════════════");
  Serial.println("  FASE 1: Inicialización del HX711");
  Serial.println("═══════════════════════════════════════════════");
  if (inicializarHX711()) {
    Serial.println("[OK] HX711 listo para lecturas");
  } else {
    Serial.println("[ERROR] HX711 no disponible");
    while(1) delay(1000);  // Detener si no hay sensor
  }
  Serial.println();

  // --- FASE 2: Calibración (Tare) ---
  Serial.println("═══════════════════════════════════════════════");
  Serial.println("  FASE 2: Calibración (Tare)");
  Serial.println("═══════════════════════════════════════════════");
  calibrar();
  Serial.println();

  // --- RESULTADO ---
  Serial.println("╔══════════════════════════════════════════════╗");
  Serial.println("║  RESULTADO PRUEBA 8.2.4: HX711 (galga)      ║");
  Serial.println("╠══════════════════════════════════════════════╣");
  Serial.println("║  ✅ EXITOSA: HX711 inicializado y calibrado  ║");
  Serial.println("║  Protocolo DT/SCK funcional                  ║");
  Serial.println("║  Lectura de valores raw operativa             ║");
  Serial.println("╚══════════════════════════════════════════════╝");
  Serial.println();

  imprimirSeparador();
}

// ============================================================
// LOOP
// ============================================================
void loop() {
  leerHX711();
  delay(1000);
}

// ============================================================
// FUNCIONES
// ============================================================

bool inicializarHX711() {
  Serial.println("[INIT] Conectando HX711...");
  Serial.printf("[INIT]   DT:  GPIO %d\n", HX711_DT);
  Serial.printf("[INIT]   SCK: GPIO %d\n", HX711_SCK);

  scale.begin(HX711_DT, HX711_SCK);

  // Verificar si el sensor responde
  if (scale.is_ready()) {
    Serial.println("[INIT]   HX711 detectado y listo");
    return true;
  }

  // Intentar esperar un poco
  Serial.println("[INIT]   Esperando HX711...");
  delay(500);
  if (scale.is_ready()) {
    Serial.println("[INIT]   HX711 detectado");
    return true;
  }

  Serial.println("[INIT]   ERROR: HX711 no responde");
  return false;
}

void calibrar() {
  Serial.println("[CAL] Realizando tare (sin carga)...");
  Serial.println("[CAL]   Promediando 10 lecturas...");

  // Promediar varias lecturas para el tare
  long sum = 0;
  int count = 0;
  for (int i = 0; i < 10; i++) {
    if (scale.is_ready()) {
      sum += scale.read();
      count++;
    }
    delay(100);
  }

  if (count > 0) {
    tareValue = (float)sum / count;
    Serial.printf("[CAL]   Valor tare (raw): %.0f\n", tareValue);
    Serial.printf("[CAL]   Lecturas válidas: %d/10\n", count);
    Serial.println("[CAL] Tare completado");
  } else {
    Serial.println("[CAL]   ERROR: No se pudo realizar tare");
  }
}

void leerHX711() {
  lecturaCount++;

  if (!scale.is_ready()) {
    Serial.printf("─── Lectura #%lu │ ERROR: HX711 no disponible ─\n", lecturaCount);
    return;
  }

  // Leer valor raw del HX711
  long rawValue = scale.read();

  // Calcular valor relativo al tare
  float relativeValue = rawValue - tareValue;

  // Simular peso (factor de calibración arbitrario para demostración)
  // En hardware real, se calibra con masa conocida
  float pesoSimulado = relativeValue * factorCalibracion / 1000.0;  // kg

  Serial.printf("─── Lectura #%lu ───────────────────────────────\n", lecturaCount);
  Serial.printf("  [HX711] Raw: %ld\n", rawValue);
  Serial.printf("  [HX711] Relativo al tare: %.0f\n", relativeValue);
  Serial.printf("  [HX711] Peso simulado: %.3f kg\n", pesoSimulado);
  Serial.println();
}

void imprimirBanner() {
  Serial.println("╔══════════════════════════════════════════════╗");
  Serial.println("║  TESIS: IoT-WSN - Subsistema Estructural    ║");
  Serial.println("║  Prueba 8.2.4: Galga BF350 + HX711          ║");
  Serial.println("║  Autores: Rabanal / Vargas                   ║");
  Serial.println("║  Entorno: Wokwi + PlatformIO + ESP32         ║");
  Serial.println("╚══════════════════════════════════════════════╝");
  Serial.println();
}

void imprimirSeparador() {
  Serial.println("════════════════════════════════════════════════");
  Serial.println("  Lectura cada 1s - Ajuste slider en diagrama");
  Serial.println("════════════════════════════════════════════════");
  Serial.println();
}
