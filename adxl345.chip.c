// ============================================================
// Custom Chip: ADXL345 - Acelerómetro MEMS 3 ejes
// Simulación para tesis: Sistema IoT-WSN - Subsistema Estructural
// Autores: Gerardo Rabanal / Gilmar Vargas
// ============================================================
// Este chip simula el protocolo I2C del ADXL345 real:
//   - Dirección I2C: 0x53
//   - Device ID: 0xE5 (registro 0x00)
//   - Registros de aceleración: 0x32-0x37
//   - Sensibilidad: 256 LSB/g (modo ±2g, 10-bit)
//   - Soporta auto-incremento de registros
// ============================================================

#include "wokwi-api.h"
#include <stdio.h>
#include <stdlib.h>

// Dirección I2C del ADXL345
#define ADXL345_ADDR 0x53

// Registros del ADXL345
#define REG_DEVID       0x00
#define REG_THRESH_TAP  0x1D
#define REG_OFSX        0x1E
#define REG_OFSY        0x1F
#define REG_OFSZ        0x20
#define REG_DUR         0x21
#define REG_LATENT      0x22
#define REG_WINDOW      0x23
#define REG_THRESH_ACT  0x24
#define REG_THRESH_INACT 0x25
#define REG_TIME_INACT  0x26
#define REG_ACT_INACT_CTL 0x27
#define REG_THRESH_FF   0x28
#define REG_TIME_FF     0x29
#define REG_TAP_AXES    0x2A
#define REG_ACT_TAP_STATUS 0x2B
#define REG_BW_RATE     0x2C
#define REG_POWER_CTL   0x2D
#define REG_INT_ENABLE  0x2E
#define REG_INT_MAP     0x2F
#define REG_INT_SOURCE  0x30
#define REG_DATA_FORMAT 0x31
#define REG_DATAX0      0x32
#define REG_DATAX1      0x33
#define REG_DATAY0      0x34
#define REG_DATAY1      0x35
#define REG_DATAZ0      0x36
#define REG_DATAZ1      0x37
#define REG_FIFO_CTL    0x38
#define REG_FIFO_STATUS 0x39

// Sensibilidad: 256 LSB/g en modo ±2g (10-bit)
// Para modo full-res: 256 LSB/g en todos los rangos
#define SENSITIVITY_LSB_PER_G 256.0

typedef struct {
  // Atributos configurables desde diagram.json
  uint32_t attr_accel_x;
  uint32_t attr_accel_y;
  uint32_t attr_accel_z;

  // Estado interno del chip
  uint8_t registers[64];  // Banco de registros
  uint8_t reg_pointer;    // Puntero de registro actual
  bool measuring;         // Estado de medición

  // Configuración I2C
  i2c_dev_t i2c;
} chip_state_t;

// Callback: el maestro I2C se dirige a este chip
static bool on_i2c_connect(void *user_data, uint32_t address, bool read) {
  chip_state_t *chip = (chip_state_t *)user_data;
  // Siempre ACK cuando la dirección coincide
  return true;
}

// Callback: el maestro envía un byte (escritura)
static bool on_i2c_write(void *user_data, uint8_t data) {
  chip_state_t *chip = (chip_state_t *)user_data;

  if (chip->reg_pointer == 0xFF) {
    // Primer byte después de START: es el puntero de registro
    chip->reg_pointer = data;
  } else {
    // Bytes subsiguientes: escribir en el registro apuntado
    if (chip->reg_pointer < sizeof(chip->registers)) {
      chip->registers[chip->reg_pointer] = data;

      // Si se escribe en POWER_CTL con bit 3 (Measure) activado
      if (chip->reg_pointer == REG_POWER_CTL && (data & 0x08)) {
        chip->measuring = true;
        // Actualizar datos de aceleración desde los atributos
        float ax = attr_read_float(chip->attr_accel_x);
        float ay = attr_read_float(chip->attr_accel_y);
        float az = attr_read_float(chip->attr_accel_z);

        // Convertir g a raw 16-bit (little-endian, dos complemento)
        int16_t raw_x = (int16_t)(ax * SENSITIVITY_LSB_PER_G);
        int16_t raw_y = (int16_t)(ay * SENSITIVITY_LSB_PER_G);
        int16_t raw_z = (int16_t)(az * SENSITIVITY_LSB_PER_G);

        // Almacenar en registros de datos (little-endian)
        chip->registers[REG_DATAX0] = raw_x & 0xFF;
        chip->registers[REG_DATAX1] = (raw_x >> 8) & 0xFF;
        chip->registers[REG_DATAY0] = raw_y & 0xFF;
        chip->registers[REG_DATAY1] = (raw_y >> 8) & 0xFF;
        chip->registers[REG_DATAZ0] = raw_z & 0xFF;
        chip->registers[REG_DATAZ1] = (raw_z >> 8) & 0xFF;

        // INT_SOURCE: Data Ready (bit 7)
        chip->registers[REG_INT_SOURCE] = 0x80;
      }
    }
    // Auto-incremento del puntero
    chip->reg_pointer++;
  }
  return true;  // ACK
}

// Callback: el maestro solicita un byte (lectura)
static uint8_t on_i2c_read(void *user_data) {
  chip_state_t *chip = (chip_state_t *)user_data;
  uint8_t value = 0;

  if (chip->reg_pointer < sizeof(chip->registers)) {
    value = chip->registers[chip->reg_pointer];
    // Auto-incremento del puntero
    chip->reg_pointer++;
  }

  return value;
}

// Callback: fin de transacción I2C
static void on_i2c_disconnect(void *user_data) {
  chip_state_t *chip = (chip_state_t *)user_data;
  // Resetear el puntero para la próxima transacción
  chip->reg_pointer = 0xFF;
}

// Inicialización del chip
void chip_init(void) {
  chip_state_t *chip = calloc(1, sizeof(chip_state_t));

  // Leer atributos configurables
  chip->attr_accel_x = attr_init_float("accelX", 0.0);
  chip->attr_accel_y = attr_init_float("accelY", 0.0);
  chip->attr_accel_z = attr_init_float("accelZ", 1.0);

  // Inicializar registros con valores por defecto del ADXL345 real
  chip->registers[REG_DEVID] = 0xE5;        // Device ID del ADXL345
  chip->registers[REG_BW_RATE] = 0x0A;      // 100 Hz ODR
  chip->registers[REG_POWER_CTL] = 0x00;    // Standby
  chip->registers[REG_DATA_FORMAT] = 0x00;  // ±2g, right-justified
  chip->registers[REG_FIFO_STATUS] = 0x00;  // FIFO vacío

  chip->reg_pointer = 0xFF;
  chip->measuring = false;

  // Configurar I2C
  const i2c_config_t i2c_config = {
    .address = ADXL345_ADDR,
    .scl = pin_init("SCL", INPUT_PULLUP),
    .sda = pin_init("SDA", INPUT_PULLUP),
    .connect = on_i2c_connect,
    .read = on_i2c_read,
    .write = on_i2c_write,
    .disconnect = on_i2c_disconnect,
    .user_data = chip,
  };
  chip->i2c = i2c_init(&i2c_config);

  printf("ADXL345 Custom Chip inicializado (addr=0x%02X)\n", ADXL345_ADDR);
}
