"""Captura el arranque del ESP32 y clasifica lo que emite por el puerto serie.

Uso: .venv/bin/python tests/sniff_boot.py [segundos]
"""

from __future__ import annotations

import collections
import sys
import time

import serial

PORT = "/dev/cu.usbserial-0001"
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0

ser = serial.Serial(PORT, 115200, timeout=0.2)
# Reset por hardware (circuito auto-reset: RTS=EN, DTR=GPIO0) para capturar el
# banner de arranque y el escaneo [SCAN] del bus I2C.
ser.setDTR(False)
ser.setRTS(True)
time.sleep(0.1)
ser.setRTS(False)
time.sleep(0.2)
t0 = time.time()
counts: collections.Counter = collections.Counter()
boot_lines: list[str] = []      # banner + escaneo + init de sensores
diag: list[str] = []            # primeras líneas de error/diagnóstico
wire_errs: list[str] = []       # errores de la capa I2C del core de Arduino
data_samples: list[str] = []    # primeras líneas DATA válidas
adxl_samples: list[str] = []    # primeras líneas ADXL
raw_path = sys.argv[2] if len(sys.argv) > 2 else None
raw = open(raw_path, "w") if raw_path else None

while time.time() - t0 < SECS:
    line = ser.readline().decode("utf-8", errors="replace").strip()
    if not line:
        continue
    if raw:
        raw.write(line + "\n")
    if line.startswith("DATA,"):
        counts["DATA (MPU válido)"] += 1
        if len(data_samples) < 3:
            data_samples.append(line)
    elif line.startswith("ADXL,"):
        counts["ADXL (ADXL345)"] += 1
        if len(adxl_samples) < 2:
            adxl_samples.append(line)
    elif line.startswith("["):
        counts["eventos"] += 1
        if "INVALIDAS" in line or "I2C ERROR" in line:
            counts["errores MPU/ADXL"] += 1
            if len(diag) < 8:
                diag.append(line)
        elif len(boot_lines) < 30:
            boot_lines.append(line)
    elif "Wire.cpp" in line or "i2cWrite" in line:
        counts["errores Wire (capa I2C)"] += 1
        if len(wire_errs) < 5:
            wire_errs.append(line)
    else:
        counts["otros (banner/bootloader)"] += 1
        # El firmware imprime el escaneo con sangría ("  Encontrado: 0x53"):
        # sin '[' inicial, pero es información de arranque que interesa ver.
        if "Encontrado" in line:
            boot_lines.append(line)

ser.close()
if raw:
    raw.close()

print("═══ ARRANQUE / ESCANEO ═══")
for l in boot_lines:
    print("  " + l)
print("═══ MUESTRAS DATA (MPU con datos válidos) ═══")
for l in data_samples:
    print("  " + l)
print("═══ MUESTRAS ADXL ═══")
for l in adxl_samples:
    print("  " + l)
print("═══ DIAGNÓSTICO DE ERRORES (primeras 8) ═══")
for l in diag or ["(ninguno)"]:
    print("  " + l)
print("═══ ERRORES WIRE / I2C (primeros 5) ═══")
for l in wire_errs or ["(ninguno)"]:
    print("  " + l)
print("═══ CONTEO EN %.0f s ═══" % SECS)
for k, v in counts.items():
    print(f"  {k}: {v}")
