# =============================================================
#  Configuración central del sistema gemelo digital
#  Importar desde cualquier capa: import config
#
#  NOTA para Capa 1 (RPi Zero 2W): copiar este archivo al
#  directorio de sensor.py en la RPi Zero durante el deploy.
#
#  Secretos (INFLUX_TOKEN): definir en archivo .env
#  (ver .env.example) — nunca hardcodear en el código.
# =============================================================

# ── Red / MQTT ───────────────────────────────────────────────
MQTT_BROKER_SENSOR = "192.168.1.104"   # IP del gemelo — usado por sensor (Capa 1)
MQTT_BROKER_GEMELO = "localhost"       # IP local — usado por suscriptor y modelo (Capas 2-3)
MQTT_PORT          = 1883

MQTT_TOPIC_DATOS      = "tanque/datos"
MQTT_TOPIC_CMD_SENSOR = "tanque/cmd"
MQTT_TOPIC_CMD_MODELO = "modelo/cmd"

# ── InfluxDB ─────────────────────────────────────────────────
# En Docker el servicio sobreescribe INFLUX_URL con la variable de entorno.
INFLUX_URL    = "http://localhost:8086"
INFLUX_ORG    = "uach"
INFLUX_BUCKET = "gemelo"
# INFLUX_TOKEN: NO va aquí — leer con os.environ.get("INFLUX_TOKEN")

# ── Geometría del tanque ──────────────────────────────────────
TANQUE_R_M = 0.141    # radio nominal [m] — usado por el solver térmico
TANQUE_H_M = 0.366    # altura [m]

# Geometría real medida (tanque es un frustum cónico, no cilindro perfecto)
# Mediciones: altura [m] → radio interno [m]
TANQUE_R_MEDICIONES_Z_M = [0.038, 0.190, 0.380]
TANQUE_R_MEDICIONES_R_M = [0.133, 0.139, 0.1445]

# ── Pared del tanque (resistencia térmica) ────────────────────
# Prototipo: HDPE ~3 mm, k=0.45 W/(m·°C)
# Tanque real (acero inox): e=0.005 m, k=16 W/(m·°C)
TANQUE_PARED_ESPESOR_M = 0.003   # [m]
TANQUE_PARED_K         = 0.45    # [W/(m·°C)] — conductividad térmica del material

# ── Capa 1 — Sensor (RPi Zero 2W) ───────────────────────────
INTERVALO_SENSOR_S = 10   # segundos entre publicaciones MQTT

PIN_TRIG   = 24           # GPIO BCM — HC-SR04
PIN_ECHO   = 25
PIN_BOMBA  = 21           # GPIO BCM — pin físico 40 — relé bomba (HIGH = activo)
PIN_FLUJO_ENTRADA = 27    # GPIO BCM — pin físico 13 — YF-S021 entrada tanque
PIN_FLUJO_SALIDA  = 22    # GPIO BCM — pin físico 15 — YF-S021 salida tanque
FLUJO_PULSOS_POR_LITRO_ENTRADA = 478   # YF-S021 entrada — calibrado experimentalmente
FLUJO_PULSOS_POR_LITRO_SALIDA  = 331   # YF-S021 salida  — ajustado por balance (con flexible)

ALTURA_CM    = 36.6       # altura máxima del tanque [cm] para HC-SR04
HX711_FACTOR = 23850      # unidades por kg — calibración celda de carga
TARA_FILE    = "/home/sebar/sensor/tara.txt"

BOMBA_DURACION_LLENADO_MIN = 5.333   # minutos para llenar el tanque superior

# IDs físicos de los sensores DS18B20 en la pared (DS0=base → DS4=tope)
DS_PARED_IDS = [
    "00000095fc87",  # DS0 —  0.0 cm
    "01215cbf83da",  # DS1 —  7.5 cm
    "01215caa0b06",  # DS2 — 15.0 cm
    "01215ceeecc6",  # DS3 — 22.5 cm
    "01215cb4d462",  # DS4 — 30.0 cm
]
DS_PARED_POSICIONES_CM = [0.0, 7.5, 15.0, 22.5, 30.0]

DS_AMB1_ID = "2ce8f30a6461"   # temperatura ambiente — sensor 1
DS_AMB2_ID = "b5d9f30a6461"   # temperatura ambiente — sensor 2
DS_SUP_ID  = "01215cd8d6d3"   # temperatura fluido en tanque superior
DS_INT_ID  = "01215cc6aad0"   # temperatura interior del tanque (encapsulado sumergible)

# ── Capa 3 — Modelo (RPi 5 / servidor) ───────────────────────
INTERVALO_MODELO_S = 10   # segundos entre iteraciones del loop

MODELO_NR = 15    # nodos radiales
MODELO_NZ = 20    # nodos axiales

MODELO_H_EXT   = 5.0   # coef. convección exterior [W/(m²·°C)] — pendiente calibrar
MODELO_ALPHA_K = 0.6   # ganancia asimilación de datos (0=solo modelo, 1=solo sensor)

MODELO_FLUIDO_DEFAULT = "aceite"   # "aceite" | "agua"
MODELO_IC_DEFAULT     = "t_sup"    # condición inicial al arrancar: "t_sup" | "sensores"
