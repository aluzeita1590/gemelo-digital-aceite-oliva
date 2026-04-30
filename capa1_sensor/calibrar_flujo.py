#!/usr/bin/env python3
"""
calibrar_flujo.py — Calibración volumétrica del sensor YF-S021
Ejecutar en la Raspberry Pi Zero 2 W con sensor.py DETENIDO.

Procedimiento:
  1. Conecta el recipiente de 1 L a la salida del sensor.
  2. Corre este script: python3 calibrar_flujo.py
  3. Sigue las instrucciones en pantalla (N ensayos de 1 L cada uno).
  4. Copia el valor obtenido en config.py → FLUJO_PULSOS_POR_LITRO.

Para calibrar el sensor de SALIDA cambia PIN a config.PIN_FLUJO_SALIDA (22).
"""

import RPi.GPIO as GPIO
import time
import statistics

# ── Parámetros — ajustar si es necesario ──────────────
PIN          = 22       # GPIO BCM — entrada (PIN_FLUJO_ENTRADA en config.py)
REPETICIONES = 5        # ensayos; mínimo 3
VOLUMEN_L    = 1.000    # litros vertidos en cada ensayo
# ──────────────────────────────────────────────────────

GPIO.setmode(GPIO.BCM)
GPIO.setup(PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

_pulsos = 0

def _contar(channel):
    global _pulsos
    _pulsos += 1

GPIO.add_event_detect(PIN, GPIO.RISING, callback=_contar, bouncetime=2)

factores = []

print()
print("=" * 52)
print("   Calibración YF-S021  —  recipiente 1 L")
print("=" * 52)
print(f"   Sensor: GPIO BCM {PIN}")
print(f"   Ensayos: {REPETICIONES}  |  Volumen por ensayo: {VOLUMEN_L} L")
print("=" * 52)

for i in range(1, REPETICIONES + 1):
    _pulsos = 0
    print(f"\n  Ensayo {i}/{REPETICIONES}")
    input("  → Coloca el recipiente vacío y presiona Enter para comenzar...")
    print("  Contando... abre el flujo ahora.")

    input("  → Cierra el flujo cuando el recipiente tenga exactamente 1 L "
          "y presiona Enter.")

    n = _pulsos
    if n == 0:
        print("  ⚠  Sin pulsos detectados. Verifica el cableado y repite.")
        factores.append(None)
        continue

    k = n / VOLUMEN_L
    factores.append(k)
    print(f"  Pulsos: {n}   →   factor = {k:.1f} pulsos/L")

# Filtrar ensayos fallidos
factores_validos = [f for f in factores if f is not None]

print()
print("=" * 52)
print("   RESULTADO")
print("=" * 52)

if len(factores_validos) < 2:
    print("  ⚠  Datos insuficientes para calcular estadísticas.")
else:
    promedio = statistics.mean(factores_validos)
    desv     = statistics.stdev(factores_validos)
    cv       = 100 * desv / promedio

    print(f"  Ensayos válidos : {len(factores_validos)}/{REPETICIONES}")
    print(f"  Factores        : {[round(f, 1) for f in factores_validos]}")
    print(f"  Promedio        : {promedio:.1f} pulsos/L")
    print(f"  Desv. estándar  : {desv:.1f} pulsos/L  (CV = {cv:.1f} %)")
    print()
    print("  Actualizar en config.py  →  línea 40:")
    print()
    print(f"    FLUJO_PULSOS_POR_LITRO = {round(promedio)}")
    print()
    if cv > 5:
        print("  ⚠  CV > 5 % — considera repetir con flujo más constante.")

print("=" * 52)

GPIO.cleanup()
