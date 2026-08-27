// wokwi-api.h — Minimal header for Wokwi Custom Chips API
// Source: https://docs.wokwi.com/chips-api
// This file provides type definitions and function declarations
// needed to compile custom chips for the Wokwi simulator.

#ifndef WOKWI_API_H
#define WOKWI_API_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

// --- Pin types and constants ---
typedef uint32_t pin_t;

#define INPUT        0
#define OUTPUT       1
#define INPUT_PULLUP 2
#define INPUT_PULLDOWN 3
#define OUTPUT_LOW   4
#define OUTPUT_HIGH  5
#define ANALOG       6

#define LOW  0
#define HIGH 1

// --- Edge detection constants ---
#define RISING  1
#define FALLING 2
#define BOTH    3

// --- Timer types ---
typedef uint32_t timer_t;

// --- I2C types ---
typedef uint32_t i2c_dev_t;

typedef bool (*i2c_connect_cb)(void *user_data, uint32_t address, bool read);
typedef uint8_t (*i2c_read_cb)(void *user_data);
typedef bool (*i2c_write_cb)(void *user_data, uint8_t data);
typedef void (*i2c_disconnect_cb)(void *user_data);

typedef struct {
  uint32_t address;
  pin_t scl;
  pin_t sda;
  i2c_connect_cb connect;
  i2c_read_cb read;
  i2c_write_cb write;
  i2c_disconnect_cb disconnect;
  void *user_data;
} i2c_config_t;

// --- SPI types ---
typedef uint32_t spi_dev_t;

typedef bool (*spi_connect_cb)(void *user_data, uint32_t address);
typedef uint8_t (*spi_read_cb)(void *user_data);
typedef bool (*spi_write_cb)(void *user_data, uint8_t data);
typedef void (*spi_disconnect_cb)(void *user_data);

typedef struct {
  pin_t sck;
  pin_t mosi;
  pin_t miso;
  pin_t cs;
  spi_connect_cb connect;
  spi_read_cb read;
  spi_write_cb write;
  spi_disconnect_cb disconnect;
  void *user_data;
} spi_config_t;

// --- Pin watch types ---
typedef void (*pin_watch_cb)(void *user_data, pin_t pin, uint32_t value);

typedef struct {
  uint32_t edge;
  pin_watch_cb pin_change;
  void *user_data;
} pin_watch_config_t;

// --- Timer callback ---
typedef void (*timer_cb)(void *user_data);

typedef struct {
  timer_cb callback;
  void *user_data;
} timer_config_t;

// --- Buffer type ---
typedef uint32_t buffer_t;

// --- GPIO functions ---
pin_t pin_init(const char *name, uint32_t mode);
void pin_mode(pin_t pin, uint32_t mode);
void pin_write(pin_t pin, uint32_t value);
uint32_t pin_read(pin_t pin);
bool pin_watch(pin_t pin, pin_watch_config_t *config);
void pin_watch_stop(pin_t pin);

// --- Analog functions ---
float pin_adc_read(pin_t pin);
void pin_dac_write(pin_t pin, float voltage);

// --- I2C functions ---
i2c_dev_t i2c_init(i2c_config_t *config);

// --- SPI functions ---
spi_dev_t spi_init(spi_config_t *config);

// --- Timer functions ---
timer_t timer_init(timer_config_t *config);
void timer_start(timer_t timer_id, uint32_t micros, bool repeat);
void timer_start_ns(timer_t timer_id, uint64_t nanos, bool repeat);
void timer_stop(timer_t timer_id);

// --- Attribute functions ---
uint32_t attr_init(const char *name, uint32_t default_value);
uint32_t attr_init_float(const char *name, float default_value);
uint32_t attr_read(uint32_t attr);
float attr_read_float(uint32_t attr);

// --- Time functions ---
uint64_t get_sim_nanos(void);

// --- Buffer functions ---
buffer_t buffer_init(const char *name, uint32_t size);
uint8_t* buffer_data(buffer_t buffer);
uint32_t buffer_size(buffer_t buffer);

#ifdef __cplusplus
}
#endif

#endif // WOKWI_API_H
