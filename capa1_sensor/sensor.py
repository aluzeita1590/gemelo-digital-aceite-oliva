import os
import sys
import time
import json
import paho.mqtt.client as mqtt
import RPi.GPIO as GPIO
from w1thermsensor import W1ThermSensor
from hx711 import HX711
from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from luma.core.render import canvas

# config.py debe estar en el mismo directorio que sensor.py en la RPi Zero
import config

# ── Configuración (desde config.py) ───────────────────
MQTT_BROKER  = config.MQTT_BROKER_SENSOR
MQTT_PORT    = config.MQTT_PORT
MQTT_TOPIC   = config.MQTT_TOPIC_DATOS
INTERVALO_S  = config.INTERVALO_SENSOR_S

# ── Pines GPIO (BCM) ──────────────────────────────────
PIN_TRIG     = config.PIN_TRIG
PIN_ECHO     = config.PIN_ECHO
ALTURA_CM    = config.ALTURA_CM

# ── Sensores ──────────────────────────────────────────
SENSOR_IDS     = config.DS_PARED_IDS
SENSOR_AMB1_ID = config.DS_AMB1_ID
SENSOR_AMB2_ID = config.DS_AMB2_ID
SENSOR_SUP_ID  = config.DS_SUP_ID

# ── Calibración HX711 ─────────────────────────────────
HX711_FACTOR = config.HX711_FACTOR

# ── Display OLED ──────────────────────────────────────
try:
    serial = i2c(port=1, address=0x3C)
    oled   = ssd1306(serial)
    DISPLAY_OK = True
    print("Display OLED detectado.")
except Exception as e:
    oled = None
    DISPLAY_OK = False
    print(f"Display no disponible: {e} — continuando sin display.")
# ── GPIO ──────────────────────────────────────────────
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
GPIO.setup(PIN_TRIG, GPIO.OUT)
GPIO.setup(PIN_ECHO, GPIO.IN)
GPIO.output(PIN_TRIG, False)

# ── HX711 ─────────────────────────────────────────────
hx = HX711(dout_pin=9, pd_sck_pin=11)
hx.reset()
time.sleep(0.5)

# ── Tara ──────────────────────────────────────────────
TARA_FILE = config.TARA_FILE

def medir_tara():
    print("Midiendo tara...")
    muestras = []
    for _ in range(20):
        datos = hx.get_raw_data(times=5)
        muestras.append(sum(datos) / len(datos))
    tara = sum(muestras) / len(muestras)
    with open(TARA_FILE, "w") as f:
        f.write(str(tara))
    print(f"Tara guardada: {tara:.0f}")
    return tara

def cargar_tara():
    if os.path.exists(TARA_FILE):
        with open(TARA_FILE, "r") as f:
            tara = float(f.read().strip())
        print(f"Tara cargada desde archivo: {tara:.0f}")
        return tara
    else:
        print("No hay tara guardada — midiendo con tanque vacío...")
        return medir_tara()

TARA = cargar_tara()

# ── Sensores temperatura ──────────────────────────────
sensores = W1ThermSensor.get_available_sensors()

# ── MQTT ──────────────────────────────────────────────
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
ciclo       = 0
ultimo      = {}
mqtt_ok     = False

def on_connect(client, userdata, flags, rc, properties):
    global mqtt_ok
    mqtt_ok = (rc == 0)
    if mqtt_ok:
        client.subscribe("tanque/cmd")

def on_message(client, userdata, msg):
    global TARA
    comando = msg.payload.decode().strip()
    if comando == "tara":
        print("Comando recibido: rehaciendo tara...")
        TARA = medir_tara()
        print("Tara actualizada.")

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
mqtt_client.loop_start()

# ── Funciones de lectura ──────────────────────────────
def leer_temperaturas():
    temps = []
    for sid in SENSOR_IDS:
        encontrado = False
        for s in sensores:
            if s.id == sid:
                try:
                    temps.append(round(s.get_temperature(), 3))
                except:
                    temps.append(None)
                encontrado = True
                break
        if not encontrado:
            temps.append(None)
    return temps

def leer_temperatura_ambiente():
    """Promedio de los dos sensores de ambiente."""
    vals = []
    for s in sensores:
        if s.id in [SENSOR_AMB1_ID, SENSOR_AMB2_ID]:
            try:
                vals.append(s.get_temperature())
            except:
                pass
    return round(sum(vals)/len(vals), 3) if vals else None

def leer_temperatura_superior():
    """Temperatura del fluido en el tanque superior."""
    for s in sensores:
        if s.id == SENSOR_SUP_ID:
            try:
                return round(s.get_temperature(), 3)
            except:
                return None
    return None

def leer_nivel():
    lecturas = []
    for _ in range(5):
        GPIO.output(PIN_TRIG, False)
        time.sleep(0.002)
        GPIO.output(PIN_TRIG, True)
        time.sleep(0.00001)
        GPIO.output(PIN_TRIG, False)
        t0 = time.time()
        while GPIO.input(PIN_ECHO) == 0:
            if time.time() - t0 > 0.1: break
        t1 = time.time()
        while GPIO.input(PIN_ECHO) == 1:
            if time.time() - t1 > 0.1: break
        t2 = time.time()
        d = (t2 - t1) * 34300 / 2
        if 1 < d < (ALTURA_CM + 5):
            lecturas.append(d)
        time.sleep(0.06)
    if not lecturas:
        return -1.0
    return round(max(0, ALTURA_CM - sum(lecturas)/len(lecturas)) / 100, 4)

def leer_masa():
    try:
        datos = hx.get_raw_data(times=10)
        valor = sum(datos) / len(datos)
        masa  = (valor - TARA) / HX711_FACTOR
        if -0.5 <= masa <= 50:
            return round(masa, 3)
        return -1.0
    except:
        return -1.0

def actualizar_display(temps, nivel, masa):
    if not DISPLAY_OK or oled is None:
        return
    t_validas = [t for t in temps if t is not None]
    t_prom    = round(sum(t_validas)/len(t_validas), 1) if t_validas else None
    nivel_cm  = round(nivel * 100, 1) if nivel >= 0 else None
    masa_str  = f"{masa:.2f} kg" if masa >= 0 else "--"
    mqtt_str  = "MQTT: OK" if mqtt_ok else "MQTT: --"

    with canvas(oled) as draw:
        draw.text((0,  0), f"T prom: {t_prom} C" if t_prom else "T prom: --", fill="white")
        draw.text((0, 16), f"Nivel:  {nivel_cm} cm" if nivel_cm else "Nivel:  --", fill="white")
        draw.text((0, 32), f"Masa:   {masa_str}", fill="white")
        draw.text((0, 48), mqtt_str, fill="white")

# ── Loop principal ────────────────────────────────────
print(f"Sensores detectados: {len(sensores)}")
print("Iniciando publicación...\n")

try:
    while True:
        ciclo += 1
        temps = leer_temperaturas()
        nivel = leer_nivel()
        masa  = leer_masa()

        actual = {"temps": temps, "nivel": nivel, "masa": masa}
        if actual != ultimo:
            actualizar_display(temps, nivel, masa)
            ultimo = actual.copy()

        t_amb = leer_temperatura_ambiente()
        t_sup = leer_temperatura_superior()
        payload = {
            "ts":         ciclo,
            "ciclo":      ciclo,
            "temp":       temps,
            "nivel_m":    nivel,
            "masa_kg":    masa,
            "n_sensores": len(sensores),
            "t_amb":      t_amb,
            "t_sup":      t_sup
        }
        mqtt_client.publish(MQTT_TOPIC, json.dumps(payload))
        print(f"Ciclo {ciclo} | T={temps} | nivel={nivel}m | masa={masa}kg")
        time.sleep(INTERVALO_S)

except KeyboardInterrupt:
    print("\nDetenido.")
    GPIO.cleanup()
    mqtt_client.loop_stop()
    oled.cleanup()
