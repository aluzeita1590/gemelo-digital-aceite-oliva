import json
import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from datetime import datetime, timezone

# ── Configuración ─────────────────────────────────────
MQTT_BROKER  = "localhost"
MQTT_PORT    = 1883
MQTT_TOPIC   = "tanque/datos"

INFLUX_URL   = "http://localhost:8086"
INFLUX_TOKEN = "3MycBr7zwAzy_L-xUsiarFMKELOqhrGqqcwJf_14YF4NmTSNePnOw5uMcwCXZQwmp3DS1JmhHeN-cEIa9TldNw=="
INFLUX_ORG   = "uach"
INFLUX_BUCKET= "gemelo"

# ── Cliente InfluxDB ──────────────────────────────────
influx  = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write   = influx.write_api(write_options=SYNCHRONOUS)

# ── Validación de rangos físicos ──────────────────────
def validar(datos):
    for t in datos.get("temp", []):
        if t is not None and not (-10 <= t <= 60):
            return False, f"Temperatura fuera de rango: {t}"
    nivel = datos.get("nivel_m", -1)
    if nivel != -1 and not (0 <= nivel <= 2):
        return False, f"Nivel fuera de rango: {nivel}"
    masa = datos.get("masa_kg", -1)
    if masa != -1 and not (-1 <= masa <= 50):
        return False, f"Masa fuera de rango: {masa}"
    return True, "OK"

# ── Callback MQTT ─────────────────────────────────────
def on_message(client, userdata, msg):
    try:
        datos = json.loads(msg.payload.decode())
        ok, motivo = validar(datos)
        if not ok:
            print(f"[DESCARTADO] {motivo}")
            return

        ts = datetime.now(timezone.utc)

        # Escribir temperaturas
        temps = datos.get("temp", [])
        for i, t in enumerate(temps):
            if t is not None:
                p = (Point("temperatura")
                     .tag("sensor", f"DS{i}")
                     .field("valor", float(t))
                     .time(ts))
                write.write(bucket=INFLUX_BUCKET, record=p)

        # Escribir temperatura ambiente
        t_amb = datos.get("t_amb")
        if t_amb is not None:
            p = (Point("temperatura")
                .tag("sensor", "DS_AMB")
                .field("valor", float(t_amb))
                .time(ts))
            write.write(bucket=INFLUX_BUCKET, record=p)

        # Escribir temperatura tanque superior
        t_sup = datos.get("t_sup")
        if t_sup is not None:
            p = (Point("temperatura")
                .tag("sensor", "DS_SUP")
                .field("valor", float(t_sup))
                .time(ts))
            write.write(bucket=INFLUX_BUCKET, record=p) 

        # Escribir nivel y masa
        if datos.get("nivel_m", -1) >= 0:
            p = (Point("nivel")
                 .field("valor", float(datos["nivel_m"]))
                 .time(ts))
            write.write(bucket=INFLUX_BUCKET, record=p)

        if datos.get("masa_kg", -1) >= 0:
            p = (Point("masa")
                 .field("valor", float(datos["masa_kg"]))
                 .time(ts))
            write.write(bucket=INFLUX_BUCKET, record=p)

        print(f"[OK] ciclo={datos.get('ciclo')} "
              f"T={temps} nivel={datos.get('nivel_m')} "
              f"masa={datos.get('masa_kg')}")

    except Exception as e:
        print(f"[ERROR] {e}")

# ── Iniciar MQTT ──────────────────────────────────────
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_message = on_message
client.connect(MQTT_BROKER, MQTT_PORT)
client.subscribe(MQTT_TOPIC)
print(f"Escuchando topic '{MQTT_TOPIC}'...")
client.loop_forever()
