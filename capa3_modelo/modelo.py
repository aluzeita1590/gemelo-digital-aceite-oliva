"""
Capa 3 — Núcleo del gemelo digital
Lee temperaturas de pared desde InfluxDB, actualiza el modelo 2D
y escribe las estimaciones T(r,z) de vuelta en InfluxDB.
Versión con condición inicial dinámica y selección de fluido por MQTT.
"""

import os
import sys
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

# Busca config.py en el mismo directorio (Docker) o en el padre (repo)
_script_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_script_dir)
_config_dir = _script_dir if os.path.exists(os.path.join(_script_dir, 'config.py')) else _parent_dir
sys.path.insert(0, _config_dir)
import config

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_config_dir, '.env'))
except ImportError:
    pass

INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN", "")
if not INFLUX_TOKEN:
    raise RuntimeError("INFLUX_TOKEN no definido. Crea un archivo .env con INFLUX_TOKEN=<token>")

# ── Configuración InfluxDB ─────────────────────────────
# INFLUX_URL puede ser sobreescrita por variable de entorno (Docker)
INFLUX_URL    = os.environ.get("INFLUX_URL", config.INFLUX_URL)
INFLUX_ORG    = config.INFLUX_ORG
INFLUX_BUCKET = config.INFLUX_BUCKET
INTERVALO_S   = config.INTERVALO_MODELO_S

# ── Configuración MQTT ─────────────────────────────────
# MQTT_BROKER puede ser sobreescrito por variable de entorno (Docker)
MQTT_BROKER = os.environ.get("MQTT_BROKER", config.MQTT_BROKER_GEMELO)
MQTT_PORT   = config.MQTT_PORT

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
FLUIDO_ACTIVO = config.MODELO_FLUIDO_DEFAULT
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
R  = config.TANQUE_R_M
H  = config.TANQUE_H_M
Nr = config.MODELO_NR
Nz = config.MODELO_NZ
dr = R / (Nr - 1)
dz = H / (Nz - 1)
r  = np.linspace(0, R, Nr)
z  = np.linspace(0, H, Nz)

# Interpolador de radio real R(z) — tanque es frustum cónico, no cilindro perfecto
_R_real_interp = interp1d(
    config.TANQUE_R_MEDICIONES_Z_M,
    config.TANQUE_R_MEDICIONES_R_M,
    kind='linear', fill_value='extrapolate', bounds_error=False
)
def R_real(z_val):
    return float(_R_real_interp(z_val))

# ── Parámetros del modelo ──────────────────────────────
h_ext   = config.MODELO_H_EXT
alpha_K = config.MODELO_ALPHA_K
T_amb   = 25.0   # temperatura ambiente [°C] — se actualiza dinámicamente

# Coeficiente global U [W/(m²·°C)]: 1/U = e_pared/k_pared + 1/h_ext
# Combina resistencia de la pared y convección exterior en serie.
_e = config.TANQUE_PARED_ESPESOR_M
_k = config.TANQUE_PARED_K
U_ext = 1.0 / (_e / _k + 1.0 / h_ext)

# dt se define dentro de cargar_fluido
dt = 30.0

# Cargar fluido inicial
cargar_fluido(FLUIDO_ACTIVO)

# ── Cliente InfluxDB ───────────────────────────────────
client    = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write     = client.write_api(write_options=SYNCHRONOUS)
query_api = client.query_api()

# ── Servidor Flask ────────────────────────────────────
app = Flask(__name__)
imagen_actual = None

def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

@app.route('/heatmap')
def heatmap():
    global imagen_actual
    if imagen_actual is None:
        return "Sin datos aún", 503
    return send_file(io.BytesIO(imagen_actual), mimetype='image/png')

@app.route('/bomba/<comando>')
def bomba_cmd(comando):
    if comando not in ("on", "off", "llenar"):
        return _cors(app.make_response(("Comando inválido", 400)))
    modelo_mqtt.publish(config.MQTT_TOPIC_CMD_SENSOR, f"bomba/{comando}")
    print(f"HTTP → bomba/{comando}")
    return _cors(app.make_response(("ok", 200)))

@app.route('/sensor/<comando>')
def sensor_cmd(comando):
    if comando not in ("tara",):
        return _cors(app.make_response(("Comando inválido", 400)))
    modelo_mqtt.publish(config.MQTT_TOPIC_CMD_SENSOR, comando)
    print(f"HTTP → sensor: {comando}")
    return _cors(app.make_response(("ok", 200)))

@app.route('/modelo/<path:comando>')
def modelo_cmd(comando):
    if comando not in ("fluido/aceite", "fluido/agua", "reset", "inicio/sup"):
        return _cors(app.make_response(("Comando inválido", 400)))
    modelo_mqtt.publish(config.MQTT_TOPIC_CMD_MODELO, comando)
    print(f"HTTP → modelo: {comando}")
    return _cors(app.make_response(("ok", 200)))

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
        escribir_condicion_inicial(T, "sensores")
    elif comando == "inicio/sup":
        t_sup = leer_t_sup()
        if t_sup is not None:
            print(f"Iniciando con T_sup={t_sup:.2f}°C (tanque superior)")
            T = np.ones((Nr, Nz)) * t_sup
            escribir_condicion_inicial(T, "t_sup")
        else:
            print("DS_SUP no disponible — usando sensores de pared")
            T = condicion_inicial_dinamica()
            escribir_condicion_inicial(T, "sensores")

modelo_mqtt = mqtt_lib.Client(mqtt_lib.CallbackAPIVersion.VERSION2)
modelo_mqtt.on_message = on_modelo_message
modelo_mqtt.connect(MQTT_BROKER, MQTT_PORT)
modelo_mqtt.subscribe(config.MQTT_TOPIC_CMD_MODELO)
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

def leer_nivel():
    """Lee el nivel del fluido desde InfluxDB (HC-SR04) en metros."""
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -2m)
      |> filter(fn: (r) => r._measurement == "nivel")
      |> filter(fn: (r) => r._field == "valor")
      |> last()
    '''
    result = query_api.query(flux)
    for table in result:
        for record in table.records:
            return float(record.get_value())
    return None

def leer_masa():
    """Lee la masa del fluido desde InfluxDB (HX711) en kg."""
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -2m)
      |> filter(fn: (r) => r._measurement == "masa")
      |> filter(fn: (r) => r._field == "valor")
      |> last()
    '''
    result = query_api.query(flux)
    for table in result:
        for record in table.records:
            return float(record.get_value())
    return None

def leer_volumen_flujo(sensor):
    """Lee el volumen acumulado [L] de un sensor de flujo (entrada o salida)."""
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -2m)
      |> filter(fn: (r) => r._measurement == "flujo")
      |> filter(fn: (r) => r.sensor == "{sensor}")
      |> filter(fn: (r) => r._field == "volumen_l")
      |> last()
    '''
    result = query_api.query(flux)
    for table in result:
        for record in table.records:
            return float(record.get_value())
    return None

def calcular_volumen_masa(T, h_nivel):
    """
    Integra nodo a nodo el campo de densidad ρ(r,z) usando la geometría
    real del tanque (frustum cónico). R(z) varía linealmente con la altura.
    Solo considera nodos con z ≤ h_nivel.
    Devuelve (V_modelo_L, M_modelo_kg, V_nivel_L).
    """
    # Volumen real desde el nivel: integra π·R(z)² sobre los nodos hasta h_nivel
    V_nivel_L = sum(
        np.pi * R_real(z[j])**2 * dz
        for j in range(Nz) if z[j] <= h_nivel
    ) * 1000.0

    V_modelo = 0.0
    M_modelo = 0.0

    for j in range(Nz):
        if z[j] > h_nivel:
            continue
        R_j  = R_real(z[j])        # radio real en esta altura
        dr_j = R_j / (Nr - 1)      # dr local para esta altura

        for i in range(Nr):
            rho_nodo = rho_0 - alpha * (T[i, j] - T_0)
            r_local  = i * dr_j    # radio real del nodo i en esta altura

            if i == 0:
                dV = np.pi * (dr_j / 2.0)**2 * dz
            else:
                dV = 2.0 * np.pi * r_local * dr_j * dz

            V_modelo += dV
            M_modelo += rho_nodo * dV

    return V_modelo * 1000.0, M_modelo, V_nivel_L  # V en litros, M en kg

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

def leer_t_int():
    """Lee la temperatura interior del tanque desde InfluxDB (DS_INT, sensor sumergible)."""
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -2m)
      |> filter(fn: (r) => r._measurement == "temperatura")
      |> filter(fn: (r) => r.sensor == "DS_INT")
      |> filter(fn: (r) => r._field == "valor")
      |> last()
    '''
    result = query_api.query(flux)
    for table in result:
        for record in table.records:
            return float(record.get_value())
    return None

# Nodo del modelo correspondiente al sensor interior (r=0, z≈19.1 cm)
_INT_J = Nz // 2   # z[Nz//2] ≈ 20.1 cm — nodo más cercano al sensor

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
    rho           = rho_0 - alpha * (T - T_0) #Densidad
    alpha_t_local = k / (rho * Cp) #Difusividad

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

    # r = R: condición Robin con resistencia de pared + convección exterior (U_ext)
    T_new[-1, :] = (T[-2, :] + dr * (U_ext / k) * T_amb) / (1 + dr * U_ext / k)

    # z = 0 y z = H: adiabático
    T_new[:, 0]  = T_new[:, 1]
    T_new[:, -1] = T_new[:, -2]

    return T_new

def escribir_condicion_inicial(T_matriz, origen):
    """Guarda en InfluxDB la temperatura media de la condición inicial."""
    p = (Point("modelo_estado")
         .tag("origen", origen)
         .field("T_ic", float(np.mean(T_matriz)))
         .time(datetime.now(timezone.utc)))
    write.write(bucket=INFLUX_BUCKET, record=p)
    print(f"IC guardada: T_ic={np.mean(T_matriz):.2f}°C (origen={origen})")

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

def generar_imagen(T, V_niv_L=None, V_mod_L=None, masa_hx=None, M_mod=None, V_bal_L=None,
                   t_int_med=None, t_int_mod=None):
    global imagen_actual
    fig, ax = plt.subplots(figsize=(6, 7))
    r_full = np.concatenate([-r[::-1], r[1:]]) * 100
    T_full = np.concatenate([T[::-1, :], T[1:, :]], axis=0)
    vmin = np.min(T)
    vmax = np.max(T)
    im = ax.contourf(r_full, z * 100, T_full.T, levels=20,
                     cmap='plasma', vmin=vmin, vmax=vmax)

    # Colorbar temperatura — derecha
    fig.colorbar(im, ax=ax, label='T [°C]', location='right', fraction=0.046, pad=0.04)

    # Colorbar densidad — izquierda
    rho_vmin = rho_0 - alpha * (vmax - T_0)
    rho_vmax = rho_0 - alpha * (vmin - T_0)
    sm_rho = plt.cm.ScalarMappable(
        cmap='plasma_r',
        norm=plt.Normalize(vmin=rho_vmin, vmax=rho_vmax)
    )
    sm_rho.set_array([])
    cb_rho = fig.colorbar(sm_rho, ax=ax, label='ρ [kg/m³]', location='left', fraction=0.046, pad=0.18)
    cb_rho.formatter = matplotlib.ticker.FormatStrFormatter('%.2f')
    cb_rho.update_ticks()

    ax.set_xlabel('Radio [cm]')
    ax.set_ylabel('Altura [cm]')
    ax.set_title(f'T(r,z) — Gemelo Digital [{FLUIDO_ACTIVO}]\n'
                 f'T_prom={np.mean(T):.2f}°C  ΔT={np.max(T)-np.min(T):.2f}°C')
    ax.axvline(0, color='white', linewidth=0.8, linestyle='--', alpha=0.6)

    # Marcador del sensor interior DS_INT en el heatmap (r=0, z=19.1 cm)
    if t_int_med is not None:
        ax.scatter([0], [z[_INT_J] * 100], marker='o', s=60,
                   color='cyan', edgecolors='white', linewidths=0.8,
                   zorder=5, label=f'DS_INT ({t_int_med:.2f}°C)')
        ax.legend(fontsize=7, loc='upper right',
                  facecolor='black', labelcolor='white', framealpha=0.6)

    # Cuadro de volumen, masa y validación interior
    lineas = []
    if V_niv_L is not None:
        lineas += [
            f'V nivel   : {V_niv_L:.2f} L',
            f'V modelo  : {V_mod_L:.2f} L',
        ]
        if V_bal_L is not None:
            lineas.append(f'V balance : {V_bal_L:.2f} L')
        if masa_hx is not None:
            lineas += [
                f'M HX711  : {masa_hx:.3f} kg',
                f'M modelo : {M_mod:.3f} kg',
            ]
    if t_int_med is not None and t_int_mod is not None:
        error = t_int_mod - t_int_med
        lineas += [
            f'T int med : {t_int_med:.2f} °C',
            f'T int mod : {t_int_mod:.2f} °C',
            f'Error int : {error:+.2f} °C',
        ]
    if lineas:
        ax.text(0.02, 0.02, '\n'.join(lineas), transform=ax.transAxes,
                fontsize=7, verticalalignment='bottom',
                bbox=dict(boxstyle='round,pad=0.4', facecolor='black',
                          alpha=0.6, edgecolor='none'),
                color='white', family='monospace')
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close()
    buf.seek(0)
    imagen_actual = buf.read()

# ── Condición inicial ──────────────────────────────────
print("Leyendo sensores para condición inicial...")
time.sleep(5)
if config.MODELO_IC_DEFAULT == "t_sup":
    t_sup_init = leer_t_sup()
    if t_sup_init is not None:
        print(f"Condición inicial: T_sup={t_sup_init:.2f}°C (tanque superior)")
        T = np.ones((Nr, Nz)) * t_sup_init
        escribir_condicion_inicial(T, "t_sup")
    else:
        print("DS_SUP no disponible — usando interpolación desde sensores de pared")
        T = condicion_inicial_dinamica()
        escribir_condicion_inicial(T, "sensores")
else:
    T = condicion_inicial_dinamica()
    escribir_condicion_inicial(T, "sensores")


# ── Loop principal ─────────────────────────────────────
print(f"Modelo 2D iniciado. Fluido: {FLUIDO_ACTIVO}. Actualizando cada {INTERVALO_S}s\n")
ciclo = 0

# Valores iniciales para el balance de masa (se capturan al arrancar)
_vol_entrada_0 = leer_volumen_flujo("entrada")
_vol_salida_0  = leer_volumen_flujo("salida")
_vol_nivel_0   = None   # se captura en el primer ciclo con datos válidos
print(f"Balance de masa — referencia inicial: "
      f"V_ent={_vol_entrada_0} L | V_sal={_vol_salida_0} L")

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

        # Volumen y masa
        h_nivel  = leer_nivel()
        masa_hx  = leer_masa()
        V_mod_L = M_mod = V_niv_L = V_bal_L = None
        if h_nivel is not None and 0 < h_nivel <= H:
            V_mod_L, M_mod, V_niv_L = calcular_volumen_masa(T, h_nivel)

            # Balance de masa: V_balance = V_nivel_inicial + ΔV_entrada - ΔV_salida
            if _vol_nivel_0 is None:
                _vol_nivel_0 = V_niv_L   # captura el nivel inicial en el primer ciclo válido
            v_ent = leer_volumen_flujo("entrada")
            v_sal = leer_volumen_flujo("salida")
            if (v_ent is not None and v_sal is not None
                    and _vol_entrada_0 is not None and _vol_salida_0 is not None):
                V_bal_L = (_vol_nivel_0
                           + (v_ent - _vol_entrada_0)
                           - (v_sal - _vol_salida_0))

            if ciclo % 6 == 0:
                ts_now = datetime.now(timezone.utc)
                p = (Point("volumen_masa")
                     .field("V_nivel_L",  round(V_niv_L, 3))
                     .field("V_modelo_L", round(V_mod_L, 3))
                     .time(ts_now))
                if masa_hx is not None:
                    p = p.field("M_hx711_kg",  round(masa_hx, 3))
                    p = p.field("M_modelo_kg", round(M_mod, 3))
                if V_bal_L is not None:
                    p = p.field("V_balance_L", round(V_bal_L, 3))
                write.write(bucket=INFLUX_BUCKET, record=p)

        # Validación interior: DS_INT vs T[0, _INT_J]
        t_int_med = leer_t_int()
        t_int_mod = float(T[0, _INT_J])
        if t_int_med is not None and ciclo % 6 == 0:
            error_int = t_int_mod - t_int_med
            ts_now = datetime.now(timezone.utc)
            p_val = (Point("validacion_interior")
                     .field("T_medida_C",  round(t_int_med, 3))
                     .field("T_modelo_C",  round(t_int_mod, 3))
                     .field("error_C",     round(error_int, 3))
                     .field("nodo_z_cm",   round(float(z[_INT_J] * 100), 1))
                     .time(ts_now))
            write.write(bucket=INFLUX_BUCKET, record=p_val)
            print(f"  DS_INT: med={t_int_med:.2f}°C | mod={t_int_mod:.2f}°C | "
                  f"err={error_int:+.2f}°C")

        generar_imagen(T, V_niv_L, V_mod_L, masa_hx, M_mod, V_bal_L,
                       t_int_med=t_int_med,
                       t_int_mod=t_int_mod if t_int_med is not None else None)

        if ciclo % 6 == 0:   # escribir en InfluxDB cada 60 segundos
            escribir_modelo(T)

        T_prom = np.mean(T)
        T_max  = np.max(T)
        T_min  = np.min(T)
        print(f"[{FLUIDO_ACTIVO}] T_prom={T_prom:.2f}°C | "
              f"T_max={T_max:.2f}°C | T_min={T_min:.2f}°C | "
              f"ΔT={T_max-T_min:.2f}°C")
        if V_niv_L is not None:
            masa_hx_str = f"{masa_hx:.3f}kg" if masa_hx is not None else "N/A"
            print(f"  V_nivel={V_niv_L:.2f}L | V_modelo={V_mod_L:.2f}L | "
                  f"M_hx711={masa_hx_str} | M_modelo={M_mod:.3f}kg")

        time.sleep(INTERVALO_S)

except KeyboardInterrupt:
    print("\nModelo detenido.")
    modelo_mqtt.loop_stop()
    client.close()
