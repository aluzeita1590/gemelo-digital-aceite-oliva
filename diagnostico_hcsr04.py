#!/usr/bin/env python3
"""
Diagnóstico HC-SR04 — muestra cada pulso crudo para detectar sensor defectuoso.
Ejecutar directamente en la RPi Zero 2W con el servicio sensor DETENIDO.

Uso:
    sudo python diagnostico_hcsr04.py

Con tanque vacío se espera d ≈ 37 cm en cada pulso.
"""
import RPi.GPIO as GPIO
import time

PIN_TRIG  = 24
PIN_ECHO  = 25
ALTURA_CM = 37.0
N_CICLOS  = 15
N_PULSOS  = 5

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
GPIO.setup(PIN_TRIG, GPIO.OUT)
GPIO.setup(PIN_ECHO, GPIO.IN)
GPIO.output(PIN_TRIG, False)
time.sleep(1)

print(f"HC-SR04  TRIG=GPIO{PIN_TRIG}  ECHO=GPIO{PIN_ECHO}")
print(f"Distancia esperada (tanque vacío): ~{ALTURA_CM:.1f} cm")
print(f"Filtro válido: 1 < d < {ALTURA_CM + 5:.1f} cm")
print("=" * 55)

try:
    for ciclo in range(1, N_CICLOS + 1):
        lecturas = []
        print(f"\nCiclo {ciclo}/{N_CICLOS}")
        for i in range(N_PULSOS):
            GPIO.output(PIN_TRIG, False)
            time.sleep(0.002)
            GPIO.output(PIN_TRIG, True)
            time.sleep(0.00001)
            GPIO.output(PIN_TRIG, False)

            t0 = time.time()
            while GPIO.input(PIN_ECHO) == 0:
                if time.time() - t0 > 0.1:
                    break
            t1 = time.time()
            while GPIO.input(PIN_ECHO) == 1:
                if time.time() - t1 > 0.1:
                    break
            t2 = time.time()

            d = (t2 - t1) * 34300 / 2
            valido = 1 < d < (ALTURA_CM + 5)
            estado = "OK  " if valido else "FAIL"

            if valido:
                lecturas.append(d)
                error = d - ALTURA_CM
                print(f"  pulso {i+1}: {d:6.1f} cm  [{estado}]  error={error:+.1f} cm")
            else:
                # Clasificar el tipo de fallo
                if d <= 1:
                    causa = "eco inmediato / pin pegado"
                elif d > ALTURA_CM + 5:
                    causa = "timeout / sin eco"
                else:
                    causa = ""
                print(f"  pulso {i+1}: {d:6.1f} cm  [{estado}]  {causa}")

            time.sleep(0.06)

        if lecturas:
            d_prom = sum(lecturas) / len(lecturas)
            nivel  = max(0, ALTURA_CM - d_prom) / 100
            print(f"  → {len(lecturas)}/5 válidas | d_prom={d_prom:.1f} cm | nivel={nivel*100:.1f} cm")
        else:
            print("  → SIN ECOS VÁLIDOS en este ciclo")

        time.sleep(2)

except KeyboardInterrupt:
    print("\nDetenido por el usuario.")
finally:
    GPIO.cleanup()
    print("GPIO liberado.")
