"""
Capa 3 — Núcleo del gemelo digital
Lee temperaturas de pared desde InfluxDB, actualiza el modelo 2D
y escribe las estimaciones T(r,z) de vuelta en InfluxDB.
Versión con condición inicial dinámica.
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

# ── Configuración InfluxDB ─────────────────────────────
INFLUX_URL    = "http://localhost:8086"
INFLUX_TOKEN  = "bADhNEbsaMqzlkCSOtoDPvuM5gxj8NR3pDmNxAWBHH4G9uey_qr-CUPn6f42E5jBmpeX5N2Mg1EmJNeNCths4g=="
INFLUX_ORG    = "uach"
INFLUX_BUCKET = "gemelo"
INTERVALO_S   = 10

# ── Propiedades del aceite (Ribeiro et al. 2017) ───────
rho_0  = 912.66
alpha  = 0.0803
T_0    = 20.0
Cp     = 1970.0
k      = 0.17

# ── Geometría del tanque prototipo 20L ─────────────────
R      = 0.141
H      = 0.366
Nr     = 15
Nz     = 20
dr     = R / (Nr - 1)
dz     = H / (Nz - 1)
r      = np.linspace(0, R, Nr)
z      = np.linspace(0, H, Nz)

# ── Parámetros del modelo ──────────────────────────────
h_ext    = 5.0
alpha_K  = 0.6
T_amb    = 25.0

# ── Estabilidad Von Neumann ────────────────────────────
rho_min  = rho_0 - alpha * (40.0 - T_0)
alpha_t  = k / (rho_min * Cp)
dt_max   = 0.25 / (alpha_t * (1/dr**2 + 1/dz**2))
dt       = min(dt_max * 0.8, 30.0)
print(f"dt = {dt:.1f} s (estabilidad garantizada)")

# ── Cliente InfluxDB ───────────────────────────────────
client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write  = client.write_api(write_options=SYNCHRONOUS)
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
print("Servidor de imagen iniciado en http://192.168.1.105:5000/heatmap")

# ── Funciones ──────────────────────────────────────────
def leer_temperaturas_pared():
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -1m)
      |> filter(fn: (r) => r._measurement == "temperatura")
      |> filter(fn: (r) => r._field == "valor")
      |> last()
    '''
    result = query_api.query(flux)
    temps = []
    for table in result:
        for record in table.records:
            temps.append(record.get_value())
    return sorted(temps) if temps else None

def condicion_inicial_dinamica():
    """Lee sensores y construye T inicial con gradiente axial real."""
    temps = leer_temperaturas_pared()
    if not temps or len(temps) < 2:
        print(f"Sin sensores — usando T_amb={T_amb}°C como condición inicial")
        return np.ones((Nr, Nz)) * T_amb
    n = len(temps)
    z_sensores = [0.0, 0.075, 0.15, 0.225, 0.30]
    T_init = np.zeros((Nr, Nz))
    for j in range(Nz):
        T_z = np.interp(z[j], z_sensores, temps)
        T_init[:, j] = T_z
    T_prom = np.mean(temps)
    print(f"Condición inicial dinámica: T_prom={T_prom:.2f}°C")
    print(f"  T_min={min(temps):.2f}°C → T_max={max(temps):.2f}°C")
    return T_init

def actualizar_con_sensores(T, temps):
    if not temps:
        return T
    n = len(temps)
    z_sensores = [0.0, 0.075, 0.15, 0.225, 0.30]
    for i, t_s in enumerate(temps):
        j = int(round(z_sensores[i] / dz))
        j = min(j, Nz - 1)
        T[-1, j] = T[-1, j] + alpha_K * (t_s - T[-1, j])
    return T

def paso_tiempo(T):
    T_new = T.copy()
    rho   = rho_0 - alpha * (T - T_0)
    alpha_t_local = k / (rho * Cp)

    T_new[1:-1, 1:-1] = T[1:-1, 1:-1] + dt * alpha_t_local[1:-1, 1:-1] * (
        (T[2:, 1:-1] - 2*T[1:-1, 1:-1] + T[:-2, 1:-1]) / dr**2 +
        (1 / r[1:-1, np.newaxis]) * (T[2:, 1:-1] - T[:-2, 1:-1]) / (2*dr) +
        (T[1:-1, 2:] - 2*T[1:-1, 1:-1] + T[1:-1, :-2]) / dz**2
    )

    T_new[0, 1:-1] = T[0, 1:-1] + dt * alpha_t_local[0, 1:-1] * (
        2 * (T[1, 1:-1] - T[0, 1:-1]) / dr**2 +
        (T[0, 2:] - 2*T[0, 1:-1] + T[0, :-2]) / dz**2
    )

    T_new[-1, :] = (T[-2, :] + dr * (h_ext / k) * T_amb) / (1 + dr * h_ext / k)
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
    ax.set_title(f'T(r,z) — Gemelo Digital\n'
                 f'T_prom={np.mean(T):.2f}°C  '
                 f'ΔT={np.max(T)-np.min(T):.2f}°C')
    ax.axvline(0, color='white', linewidth=0.8, linestyle='--', alpha=0.6)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close()
    buf.seek(0)
    imagen_actual = buf.read()

# ── Condición inicial dinámica ─────────────────────────
print("Leyendo sensores para condición inicial...")
time.sleep(3)
T = condicion_inicial_dinamica()

# ── Loop principal ─────────────────────────────────────
print("Modelo 2D iniciado. Actualizando cada 10 segundos...\n")

try:
    while True:
        temps = leer_temperaturas_pared()

        pasos = int(INTERVALO_S / dt)
        for _ in range(pasos):
            T = paso_tiempo(T)

        if temps:
            T = actualizar_con_sensores(T, temps)
            print(f"Sensores: {[round(t,2) for t in temps]}")

        escribir_modelo(T)
        generar_imagen(T)

        T_prom = np.mean(T)
        T_max  = np.max(T)
        T_min  = np.min(T)
        print(f"T_prom={T_prom:.2f}°C | T_max={T_max:.2f}°C | "
              f"T_min={T_min:.2f}°C | ΔT={T_max-T_min:.2f}°C")

        time.sleep(INTERVALO_S)

except KeyboardInterrupt:
    print("\nModelo detenido.")
    client.close()
