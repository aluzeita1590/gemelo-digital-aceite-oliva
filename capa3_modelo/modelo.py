"""
Capa 3 — Núcleo del gemelo digital
Lee temperaturas de pared desde InfluxDB, actualiza el modelo 2D
y escribe las estimaciones T(r,z) de vuelta en InfluxDB.
Versión con condición inicial dinámica y selección de fluido por MQTT.
"""

import time
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from threading import Thread
from flask import Flask, send_file
import io
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from datetime import datetime, timezone
import paho.mqtt.client as mqtt_lib
from scipy.interpolate import interp1d

# ── Configuración InfluxDB ─────────────────────────────
INFLUX_URL    = "http://localhost:8086"
INFLUX_TOKEN  = "3MycBr7zwAzy_L-xUsiarFMKELOqhrGqqcwJf_14YF4NmTSNePnOw5uMcwCXZQwmp3DS1JmhHeN-cEIa9TldNw=="
INFLUX_ORG    = "uach"
INFLUX_BUCKET = "gemelo"
INTERVALO_S   = 10

# ── Configuración MQTT ─────────────────────────────────
MQTT_BROKER = "localhost"
MQTT_PORT   = 1883

# ── Propiedades de los fluidos ─────────────────────────
FLUIDOS = {
    "aceite": {
        "rho_0": 912.66,   # kg/m³  Ribeiro et al. (2017)
        "alpha": 0.0803,   # kg/(m³·°C)
        "T_0":   20.0,     # °C
        "Cp":    1970.0,   # J/(kg·°C)  Fasina y Colley (2008)
        "k":     0.17,     # W/(m·°C)   Turgut et al. (2009)
    },
    "agua": {
        "rho_0": 998.2,    # kg/m³  a 20°C
        "alpha": 0.0975,   # kg/(m³·°C)
        "T_0":   20.0,     # °C
        "Cp":    4182.0,   # J/(kg·°C)
        "k":     0.598,    # W/(m·°C)
    }
}

# Fluido activo
FLUIDO_ACTIVO = "aceite"
rho_0 = alpha = T_0 = Cp = k = None

def cargar_fluido(nombre):
    global rho_0, alpha, T_0, Cp, k, FLUIDO_ACTIVO, dt
    if nombre not in FLUIDOS:
        print(f"Fluido '{nombre}' no reconocido. Disponibles: {list(FLUIDOS.keys())}")
        return
    props     = FLUIDOS[nombre]
    rho_0     = props["rho_0"]
    alpha     = props["alpha"]
    T_0       = props["T_0"]
    Cp        = props["Cp"]
    k         = props["k"]
    FLUIDO_ACTIVO = nombre
    # Recalcular dt con Von Neumann para el nuevo fluido
    rho_min   = rho_0 - alpha * (40.0 - T_0)
    alpha_t   = k / (rho_min * Cp)
    dt_max    = 0.25 / (alpha_t * (1/dr**2 + 1/dz**2))
    dt        = min(dt_max * 0.8, 30.0)
    print(f"Fluido: {nombre} | ρ₀={rho_0} | Cp={Cp} | k={k} | dt={dt:.1f}s")

# ── Geometría del tanque prototipo 20L ─────────────────
R  = 0.141    # radio [m]
H  = 0.366    # altura [m]
Nr = 15       # nodos radiales
Nz = 20       # nodos axiales
dr = R / (Nr - 1)
dz = H / (Nz - 1)
r  = np.linspace(0, R, Nr)
z  = np.linspace(0, H, Nz)

# ── Parámetros del modelo ──────────────────────────────
h_ext   = 5.0    # coef. convección exterior [W/(m²·°C)]
alpha_K = 0.6    # ganancia asimilación de datos
T_amb   = 25.0   # temperatura ambiente [°C]

# dt se define dentro de cargar_fluido
dt = 30.0

# Cargar fluido inicial
cargar_fluido(FLUIDO_ACTIVO)

# ── Cliente InfluxDB ───────────────────────────────────
client    = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write     = client.write_api(write_options=SYNCHRONOUS)
query_api = client.query_api()

# ── Servidor de imagen Flask ───────────────────────────
app = Flask(__name__)
imagen_actual = None

@app.route('/heatmap')
def heatmap():
    global imagen_actual
    if imagen_actual is None:
        return "Sin datos aún", 503
    return send_file(io.BytesIO(imagen_actual), mimetype='image/png')

def iniciar_servidor():
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

Thread(target=iniciar_servidor, daemon=True).start()
print("Servidor de imagen: http://192.168.1.104:5000/heatmap")

# ── Cliente MQTT para comandos ─────────────────────────
def on_modelo_message(client, userdata, msg):
    global T
    comando = msg.payload.decode().strip()
    print(f"Comando recibido: {comando}")
    if comando.startswith("fluido/"):
        nombre = comando.split("/")[1]
        cargar_fluido(nombre)
    elif comando == "reset":
        print("Reiniciando condición inicial con sensores de pared...")
        T = condicion_inicial_dinamica()
    elif comando == "inicio/sup":
        t_sup = leer_t_sup()
        if t_sup is not None:
            print(f"Iniciando con T_sup={t_sup:.2f}°C (tanque superior)")
            T = np.ones((Nr, Nz)) * t_sup
        else:
            print("DS_SUP no disponible — usando sensores de pared")
            T = condicion_inicial_dinamica()

modelo_mqtt = mqtt_lib.Client(mqtt_lib.CallbackAPIVersion.VERSION2)
modelo_mqtt.on_message = on_modelo_message
modelo_mqtt.connect(MQTT_BROKER, MQTT_PORT)
modelo_mqtt.subscribe("modelo/cmd")
modelo_mqtt.loop_start()
print("Escuchando comandos en topic 'modelo/cmd'")

# ── Funciones ──────────────────────────────────────────
# Orden físico de los sensores (DS0=base, DS4=tope a 30cm)
SENSOR_TAGS   = ["DS0", "DS1", "DS2", "DS3", "DS4"]
Z_SENSORES_M  = [0.0, 0.075, 0.15, 0.225, 0.30]

def leer_temperaturas_pared():
    """
    Lee las temperaturas de cada sensor en orden físico (DS0→DS4).
    Devuelve lista ordenada por posición, no por temperatura.
    """
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -1m)
      |> filter(fn: (r) => r._measurement == "temperatura")
      |> filter(fn: (r) => r._field == "valor")
      |> last()
    '''
    result = query_api.query(flux)
    sensor_map = {}
    for table in result:
        for record in table.records:
            tag = record.values.get("sensor", "")
            sensor_map[tag] = record.get_value()

    temps = []
    for tag in SENSOR_TAGS:
        if tag in sensor_map:
            temps.append(sensor_map[tag])

    return temps if len(temps) >= 2 else None

def leer_t_amb():
    """Lee la temperatura ambiente desde InfluxDB (sensor DS_AMB)."""
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -2m)
      |> filter(fn: (r) => r._measurement == "temperatura")
      |> filter(fn: (r) => r.sensor == "DS_AMB")
      |> filter(fn: (r) => r._field == "valor")
      |> last()
    '''
    result = query_api.query(flux)
    for table in result:
        for record in table.records:
            return float(record.get_value())
    return None

def leer_t_sup():
    """Lee la temperatura del tanque superior desde InfluxDB (DS_SUP)."""
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -2m)
      |> filter(fn: (r) => r._measurement == "temperatura")
      |> filter(fn: (r) => r.sensor == "DS_SUP")
      |> filter(fn: (r) => r._field == "valor")
      |> last()
    '''
    result = query_api.query(flux)
    for table in result:
        for record in table.records:
            return float(record.get_value())
    return None

def condicion_inicial_dinamica():
    """Lee sensores y construye T inicial con interpolación + extrapolación."""
    temps = leer_temperaturas_pared()
    if not temps or len(temps) < 2:
        print(f"Sin sensores — usando T_amb={T_amb}°C como condición inicial")
        return np.ones((Nr, Nz)) * T_amb
    interp = interp1d(Z_SENSORES_M, temps,
                      kind='linear',
                      fill_value='extrapolate',
                      bounds_error=False)
    T_init = np.zeros((Nr, Nz))
    for j in range(Nz):
        T_z = float(interp(z[j]))
        T_z = np.clip(T_z, min(temps) - 1.0, max(temps) + 1.0)
        T_init[:, j] = T_z
    T_prom = np.mean(temps)
    print(f"Condición inicial dinámica: T_prom={T_prom:.2f}°C")
    print(f"  T(DS0={temps[0]:.2f}) → T(DS4={temps[-1]:.2f}°C)")
    return T_init

def actualizar_con_sensores(T, temps):
    if not temps:
        return T
    interp = interp1d(Z_SENSORES_M, temps,
                      kind='linear',
                      fill_value='extrapolate',
                      bounds_error=False)
    for j in range(Nz):
        T_interp = float(interp(z[j]))
        T_interp = np.clip(T_interp, min(temps) - 1.0, max(temps) + 1.0)
        T[-1, j] = T[-1, j] + alpha_K * (T_interp - T[-1, j])
    return T

def paso_tiempo(T):
    T_new         = T.copy()
    rho           = rho_0 - alpha * (T - T_0)
    alpha_t_local = k / (rho * Cp)

    # Nodos interiores — Laplaciano vectorizado
    T_new[1:-1, 1:-1] = T[1:-1, 1:-1] + dt * alpha_t_local[1:-1, 1:-1] * (
        (T[2:, 1:-1] - 2*T[1:-1, 1:-1] + T[:-2, 1:-1]) / dr**2 +
        (1 / r[1:-1, np.newaxis]) * (T[2:, 1:-1] - T[:-2, 1:-1]) / (2*dr) +
        (T[1:-1, 2:] - 2*T[1:-1, 1:-1] + T[1:-1, :-2]) / dz**2
    )

    # r = 0: simetría axial con L'Hôpital
    T_new[0, 1:-1] = T[0, 1:-1] + dt * alpha_t_local[0, 1:-1] * (
        2 * (T[1, 1:-1] - T[0, 1:-1]) / dr**2 +
        (T[0, 2:] - 2*T[0, 1:-1] + T[0, :-2]) / dz**2
    )

    # r = R: condición Robin (convección exterior)
    T_new[-1, :] = (T[-2, :] + dr * (h_ext / k) * T_amb) / (1 + dr * h_ext / k)

    # z = 0 y z = H: adiabático
    T_new[:, 0]  = T_new[:, 1]
    T_new[:, -1] = T_new[:, -2]

    return T_new

def escribir_modelo(T):
    ts = datetime.now(timezone.utc)
    points = []
    for i in range(Nr):
        for j in range(Nz):
            p = (Point("temperatura_modelo")
                 .tag("nodo_r", i)
                 .tag("nodo_z", j)
                 .tag("fluido", FLUIDO_ACTIVO)
                 .field("T", float(T[i, j]))
                 .field("r_cm", float(r[i] * 100))
                 .field("z_cm", float(z[j] * 100))
                 .time(ts))
            points.append(p)
    write.write(bucket=INFLUX_BUCKET, record=points)

def generar_imagen(T):
    global imagen_actual
    fig, ax = plt.subplots(figsize=(5, 7))
    r_full = np.concatenate([-r[::-1], r[1:]]) * 100
    T_full = np.concatenate([T[::-1, :], T[1:, :]], axis=0)
    vmin = np.min(T) - 0.1
    vmax = np.max(T) + 0.1
    im = ax.contourf(r_full, z * 100, T_full.T, levels=20,
                     cmap='RdYlBu_r', vmin=vmin, vmax=vmax)
    plt.colorbar(im, ax=ax, label='T [°C]')
    ax.set_xlabel('Radio [cm]')
    ax.set_ylabel('Altura [cm]')
    ax.set_title(f'T(r,z) — Gemelo Digital [{FLUIDO_ACTIVO}]\n'
                 f'T_prom={np.mean(T):.2f}°C  ΔT={np.max(T)-np.min(T):.2f}°C')
    ax.axvline(0, color='white', linewidth=0.8, linestyle='--', alpha=0.6)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close()
    buf.seek(0)
    imagen_actual = buf.read()

# ── Condición inicial dinámica ─────────────────────────
print("Leyendo sensores para condición inicial...")
time.sleep(5)
T = condicion_inicial_dinamica()


# ── Loop principal ─────────────────────────────────────
print(f"Modelo 2D iniciado. Fluido: {FLUIDO_ACTIVO}. Actualizando cada {INTERVALO_S}s\n")
ciclo = 0

try:
    while True:
        ciclo += 1
        temps = leer_temperaturas_pared()

        # Actualizar T_amb dinámico si hay sensor de ambiente
        t_amb_nuevo = leer_t_amb()
        if t_amb_nuevo is not None:
            T_amb = t_amb_nuevo

        pasos = max(1, int(INTERVALO_S / dt))
        for _ in range(pasos):
            T = paso_tiempo(T)

        if temps:
            T = actualizar_con_sensores(T, temps)
            print(f"Sensores: {[round(t,2) for t in temps]}")

        generar_imagen(T)

        if ciclo % 6 == 0:   # escribir en InfluxDB cada 60 segundos
            escribir_modelo(T)

        T_prom = np.mean(T)
        T_max  = np.max(T)
        T_min  = np.min(T)
        print(f"[{FLUIDO_ACTIVO}] T_prom={T_prom:.2f}°C | "
              f"T_max={T_max:.2f}°C | T_min={T_min:.2f}°C | "
              f"ΔT={T_max-T_min:.2f}°C")

        time.sleep(INTERVALO_S)

except KeyboardInterrupt:
    print("\nModelo detenido.")
    modelo_mqtt.loop_stop()
    client.close()
