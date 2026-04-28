import os
import sys
import time
import json
import threading
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
PIN_TRIG          = config.PIN_TRIG
PIN_ECHO          = config.PIN_ECHO
PIN_BOMBA         = config.PIN_BOMBA
PIN_FLUJO_ENTRADA = config.PIN_FLUJO_ENTRADA
PIN_FLUJO_SALIDA  = config.PIN_FLUJO_SALIDA
PULSOS_POR_LITRO  = config.FLUJO_PULSOS_POR_LITRO
ALTURA_CM         = config.ALTURA_CM
DURACION_LLENADO_S = config.BOMBA_DURACION_LLENADO_MIN * 60

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
GPIO.setup(PIN_BOMBA, GPIO.OUT)
GPIO.output(PIN_BOMBA, GPIO.LOW)   # bomba apagada al iniciar
GPIO.setup(PIN_FLUJO_ENTRADA, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(PIN_FLUJO_SALIDA,  GPIO.IN, pull_up_down=GPIO.PUD_UP)

# ── HX711 ─────────────────────────────────────────────
hx = HX711(dout_pin=11, pd_sck_pin=9)
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

# ── Sensores de flujo YF-S021 ────────────────────────
_pulsos_entrada = 0
_pulsos_salida  = 0
vol_entrada_l   = 0.0
vol_salida_l    = 0.0

def _hilo_flujo():
    global _pulsos_entrada, _pulsos_salida
    prev_ent = GPIO.input(PIN_FLUJO_ENTRADA)
    prev_sal = GPIO.input(PIN_FLUJO_SALIDA)
    while True:
        curr_ent = GPIO.input(PIN_FLUJO_ENTRADA)
        curr_sal = GPIO.input(PIN_FLUJO_SALIDA)
        if prev_ent == 0 and curr_ent == 1:
            _pulsos_entrada += 1
        if prev_sal == 0 and curr_sal == 1:
            _pulsos_salida += 1
        prev_ent = curr_ent
        prev_sal = curr_sal
        time.sleep(0.002)

threading.Thread(target=_hilo_flujo, daemon=True).start()

def leer_flujo():
    """Lee pulsos acumulados desde el último ciclo y calcula caudal + volumen."""
    global _pulsos_entrada, _pulsos_salida, vol_entrada_l, vol_salida_l
    p_ent = _pulsos_entrada;  _pulsos_entrada = 0
    p_sal = _pulsos_salida;   _pulsos_salida  = 0
    litros_ent = p_ent / PULSOS_POR_LITRO
    litros_sal = p_sal / PULSOS_POR_LITRO
    vol_entrada_l += litros_ent
    vol_salida_l  += litros_sal
    caudal_ent = round(litros_ent / (INTERVALO_S / 60), 4)   # L/min
    caudal_sal = round(litros_sal / (INTERVALO_S / 60), 4)
    return {
        "flujo_entrada_lmin": caudal_ent,
        "flujo_salida_lmin":  caudal_sal,
        "vol_entrada_l":      round(vol_entrada_l, 4),
        "vol_salida_l":       round(vol_salida_l,  4),
    }

# ── Control de bomba ──────────────────────────────────
bomba_activa   = False
_timer_llenado = None

def activar_bomba():
    global bomba_activa
    GPIO.output(PIN_BOMBA, GPIO.HIGH)
    bomba_activa = True
    print("Bomba: ENCENDIDA")

def desactivar_bomba(notificar_modelo=False):
    global bomba_activa, _timer_llenado
    GPIO.output(PIN_BOMBA, GPIO.LOW)
    bomba_activa = False
    if _timer_llenado is not None:
        _timer_llenado.cancel()
        _timer_llenado = None
    print("Bomba: APAGADA")
    if notificar_modelo:
        mqtt_client.publish(config.MQTT_TOPIC_CMD_MODELO, "inicio/sup")
        print("Modelo: inicio/sup enviado (tanque superior lleno)")

def iniciar_llenado():
    global _timer_llenado
    if _timer_llenado is not None:
        _timer_llenado.cancel()
    activar_bomba()
    print(f"Bomba: llenado automático — {config.BOMBA_DURACION_LLENADO_MIN} min")
    _timer_llenado = threading.Timer(DURACION_LLENADO_S,
                                     lambda: desactivar_bomba(notificar_modelo=True))
    _timer_llenado.daemon = True
    _timer_llenado.start()

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
    elif comando == "bomba/on":
        activar_bomba()
    elif comando == "bomba/off":
        desactivar_bomba()
    elif comando == "bomba/llenar":
        iniciar_llenado()

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
    estado_str = ("MQTT:OK" if mqtt_ok else "MQTT:--") + \
                 ("  BOMBA:ON" if bomba_activa else "  BOMBA:--")

    with canvas(oled) as draw:
        draw.text((0,  0), f"T prom: {t_prom} C" if t_prom else "T prom: --", fill="white")
        draw.text((0, 16), f"Nivel:  {nivel_cm} cm" if nivel_cm else "Nivel:  --", fill="white")
        draw.text((0, 32), f"Masa:   {masa_str}", fill="white")
        draw.text((0, 48), estado_str, fill="white")

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
        flujo = leer_flujo()
        payload = {
            "ts":         ciclo,
            "ciclo":      ciclo,
            "temp":       temps,
            "nivel_m":    nivel,
            "masa_kg":    masa,
            "n_sensores": len(sensores),
            "t_amb":      t_amb,
            "t_sup":      t_sup,
            "bomba":      bomba_activa,
            **flujo,
        }
        mqtt_client.publish(MQTT_TOPIC, json.dumps(payload))
        print(f"Ciclo {ciclo} | T={temps} | nivel={nivel}m | masa={masa}kg")
        time.sleep(INTERVALO_S)

except KeyboardInterrupt:
    print("\nDetenido.")
    desactivar_bomba()
    GPIO.cleanup()
    mqtt_client.loop_stop()
    oled.cleanup()
