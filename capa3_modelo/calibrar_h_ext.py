"""
Calibración de h_ext — Experimento de enfriamiento natural

Procedimiento:
  1. Llenar el tanque, dejar en reposo con aire tranquilo (sin flujo, sin bomba).
  2. Ajustar T_INICIO y T_FIN al rango del experimento en este archivo.
  3. Correr: python calibrar_h_ext.py
  4. El script lee DS0–DS4 y DS_AMB de InfluxDB, corre el modelo sin asimilación
     para cada valor de H_EXT_VALORES, calcula el RMSE y muestra las curvas.
  5. Actualizar MODELO_H_EXT en config.py con el valor que minimiza el RMSE.
"""

import sys
import os
import numpy as np
import matplotlib.pyplot as plt
from influxdb_client import InfluxDBClient
from scipy.interpolate import interp1d
from datetime import datetime

_script_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_script_dir)
sys.path.insert(0, _parent_dir)
import config

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_parent_dir, '.env'))
except ImportError:
    pass

# ── Parámetros del experimento — AJUSTAR antes de correr ──────────
T_INICIO = "2026-05-01T10:00:00Z"   # inicio del experimento (ISO 8601 UTC)
T_FIN    = "2026-05-01T14:00:00Z"   # fin del experimento
FLUIDO   = "aceite"                  # "aceite" | "agua"

# Valores de h_ext a evaluar [W/(m²·°C)]
H_EXT_VALORES = [2.0, 3.0, 5.0, 8.0, 10.0, 15.0]

# ── Propiedades de fluidos ─────────────────────────────────────────
FLUIDOS = {
    "aceite": {"rho_0": 912.66, "alpha": 0.0803, "T_0": 20.0, "Cp": 1970.0, "k": 0.17},
    "agua":   {"rho_0": 998.2,  "alpha": 0.0975, "T_0": 20.0, "Cp": 4182.0, "k": 0.598},
}

# ── Geometría ──────────────────────────────────────────────────────
R  = config.TANQUE_R_M
H  = config.TANQUE_H_M
Nr = config.MODELO_NR
Nz = config.MODELO_NZ
dr = R / (Nr - 1)
dz = H / (Nz - 1)
r  = np.linspace(0, R, Nr)
z  = np.linspace(0, H, Nz)
Z_SENSORES_M = [p / 100.0 for p in config.DS_PARED_POSICIONES_CM]

_e_pared  = config.TANQUE_PARED_ESPESOR_M
_k_pared  = config.TANQUE_PARED_K

# ── InfluxDB ───────────────────────────────────────────────────────
INFLUX_TOKEN  = os.environ.get("INFLUX_TOKEN", "")
INFLUX_URL    = config.INFLUX_URL
INFLUX_ORG    = config.INFLUX_ORG
INFLUX_BUCKET = config.INFLUX_BUCKET

if not INFLUX_TOKEN:
    raise RuntimeError("INFLUX_TOKEN no definido. Revisar archivo .env")

client    = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
query_api = client.query_api()


def leer_sensor(sensor_tag):
    """Devuelve (timestamps_s, valores) para un sensor en el rango del experimento."""
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {T_INICIO}, stop: {T_FIN})
      |> filter(fn: (r) => r._measurement == "temperatura")
      |> filter(fn: (r) => r.sensor == "{sensor_tag}")
      |> filter(fn: (r) => r._field == "valor")
      |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
    '''
    result = query_api.query(flux)
    datos = sorted(
        [(rec.get_time().timestamp(), float(rec.get_value()))
         for table in result for rec in table.records],
        key=lambda x: x[0]
    )
    return np.array([x[0] for x in datos]), np.array([x[1] for x in datos])


def leer_datos_experimento():
    """
    Lee DS0–DS4 y DS_AMB. Devuelve:
      t_rel      — tiempo relativo [s] con resolución 1 min
      T_pared    — (5, N) temperaturas individuales de pared
      T_pared_mean — (N,) promedio DS0–DS4
      T_amb_arr  — (N,) temperatura ambiente interpolada en t_rel
    """
    print("Leyendo datos de InfluxDB...")

    ts_ref = None
    T_pared = []
    for i in range(5):
        ts, vs = leer_sensor(f"DS{i}")
        if ts_ref is None:
            ts_ref = ts
        f = interp1d(ts - ts[0], vs, bounds_error=False,
                     fill_value=(vs[0], vs[-1]))
        t_rel = ts_ref - ts_ref[0]
        T_pared.append(f(t_rel))
        print(f"  DS{i}: {len(ts)} puntos")

    ts_amb, vs_amb = leer_sensor("DS_AMB")
    print(f"  DS_AMB: {len(ts_amb)} puntos")

    t_rel = ts_ref - ts_ref[0]
    f_amb = interp1d(ts_amb - ts_amb[0], vs_amb, bounds_error=False,
                     fill_value=(vs_amb[0], vs_amb[-1]))
    T_amb_arr = f_amb(t_rel)

    T_pared = np.array(T_pared)
    T_pared_mean = np.mean(T_pared, axis=0)

    return t_rel, T_pared, T_pared_mean, T_amb_arr


def simular_enfriamiento(T_inicial_pared, t_exp, T_amb_arr, h_ext_val):
    """
    Corre el modelo sin asimilación de datos para un h_ext dado.
    Devuelve T_pared_sim (N,): promedio de T(r=R, z) en cada instante de t_exp.
    """
    props = FLUIDOS[FLUIDO]
    rho_0 = props["rho_0"]
    alpha  = props["alpha"]
    T_0   = props["T_0"]
    Cp    = props["Cp"]
    k     = props["k"]

    rho_min = rho_0 - alpha * (40.0 - T_0)
    alpha_t = k / (rho_min * Cp)
    dt_max  = 0.25 / (alpha_t * (1/dr**2 + 1/dz**2))
    dt      = min(dt_max * 0.8, 30.0)

    U_ext = 1.0 / (_e_pared / _k_pared + 1.0 / h_ext_val)

    # Condición inicial: interpolar lecturas iniciales de pared en z
    f_ic = interp1d(Z_SENSORES_M, T_inicial_pared,
                    kind='linear', fill_value='extrapolate', bounds_error=False)
    T = np.zeros((Nr, Nz))
    for j in range(Nz):
        T[:, j] = float(f_ic(z[j]))

    f_amb = interp1d(t_exp, T_amb_arr, bounds_error=False,
                     fill_value=(T_amb_arr[0], T_amb_arr[-1]))

    def paso(T, T_amb_val):
        T_new = T.copy()
        rho = rho_0 - alpha * (T - T_0)
        a   = k / (rho * Cp)

        T_new[1:-1, 1:-1] = T[1:-1, 1:-1] + dt * a[1:-1, 1:-1] * (
            (T[2:, 1:-1] - 2*T[1:-1, 1:-1] + T[:-2, 1:-1]) / dr**2 +
            (1/r[1:-1, np.newaxis]) * (T[2:, 1:-1] - T[:-2, 1:-1]) / (2*dr) +
            (T[1:-1, 2:] - 2*T[1:-1, 1:-1] + T[1:-1, :-2]) / dz**2
        )
        T_new[0, 1:-1] = T[0, 1:-1] + dt * a[0, 1:-1] * (
            2*(T[1, 1:-1] - T[0, 1:-1]) / dr**2 +
            (T[0, 2:] - 2*T[0, 1:-1] + T[0, :-2]) / dz**2
        )
        T_new[-1, :] = (T[-2, :] + dr * (U_ext/k) * T_amb_val) / (1 + dr * U_ext/k)
        T_new[:, 0]  = T_new[:, 1]
        T_new[:, -1] = T_new[:, -2]
        return T_new

    T_pared_sim = np.zeros(len(t_exp))
    T_pared_sim[0] = np.mean(T[-1, :])
    t_sim = 0.0

    for idx in range(1, len(t_exp)):
        while t_sim < t_exp[idx]:
            T = paso(T, float(f_amb(t_sim)))
            t_sim += dt
        T_pared_sim[idx] = np.mean(T[-1, :])

    return T_pared_sim


def main():
    t_exp, T_pared, T_pared_mean, T_amb_arr = leer_datos_experimento()

    if len(t_exp) < 5:
        print("ERROR: Pocos datos. Verificar T_INICIO y T_FIN.")
        return

    t_min = t_exp / 60.0
    T_inicial_pared = T_pared[:, 0]

    print(f"\nExperimento: {len(t_exp)} puntos | duración {t_exp[-1]/3600:.1f} h")
    print(f"T inicial pared : {np.mean(T_inicial_pared):.2f} °C")
    print(f"T final pared   : {T_pared_mean[-1]:.2f} °C")
    print(f"T ambiente prom : {np.mean(T_amb_arr):.2f} °C\n")

    # ── Simulación ──────────────────────────────────────────────
    resultados = {}
    for h in H_EXT_VALORES:
        print(f"Simulando h_ext = {h:5.1f} W/(m²·°C) ...", end=" ", flush=True)
        T_sim = simular_enfriamiento(T_inicial_pared, t_exp, T_amb_arr, h)
        rmse  = np.sqrt(np.mean((T_sim - T_pared_mean)**2))
        resultados[h] = {"T_sim": T_sim, "rmse": rmse}
        print(f"RMSE = {rmse:.4f} °C")

    h_opt = min(resultados, key=lambda h: resultados[h]["rmse"])
    print(f"\nMejor h_ext : {h_opt} W/(m²·°C)  —  RMSE = {resultados[h_opt]['rmse']:.4f} °C")
    print(f"Actualizar en config.py:  MODELO_H_EXT = {h_opt}")

    # ── Gráficos ────────────────────────────────────────────────
    colores = plt.cm.plasma(np.linspace(0.1, 0.9, len(H_EXT_VALORES)))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    # Curvas de enfriamiento
    nombres_ds = ['DS0', 'DS1', 'DS2', 'DS3', 'DS4']
    for i, nombre in enumerate(nombres_ds):
        ax1.plot(t_min, T_pared[i], '--', alpha=0.35, linewidth=0.9, label=nombre)
    ax1.plot(t_min, T_pared_mean, 'k-', linewidth=2.5, label='Promedio sensores')
    ax1.plot(t_min, T_amb_arr, 'b:', linewidth=1.2, label='T ambiente')

    for (h, res), color in zip(resultados.items(), colores):
        lw    = 2.5 if h == h_opt else 1.2
        label = f"h={h} (RMSE={res['rmse']:.3f}°C)"
        marker = ' ★' if h == h_opt else ''
        ax1.plot(t_min, res["T_sim"], color=color, linewidth=lw, label=label + marker)

    ax1.set_xlabel('Tiempo [min]')
    ax1.set_ylabel('T [°C]')
    ax1.set_title('Curvas de enfriamiento — Sensores vs Modelo')
    ax1.legend(fontsize=7, loc='upper right')
    ax1.grid(True, alpha=0.3)

    # RMSE por h_ext
    h_vals    = list(resultados.keys())
    rmse_vals = [resultados[h]["rmse"] for h in h_vals]
    idx_opt   = h_vals.index(h_opt)
    bar_colors = [colores[i] for i in range(len(h_vals))]
    bars = ax2.bar([str(h) for h in h_vals], rmse_vals, color=bar_colors, edgecolor='none')
    bars[idx_opt].set_edgecolor('black')
    bars[idx_opt].set_linewidth(2.5)
    ax2.set_xlabel('h_ext [W/(m²·°C)]')
    ax2.set_ylabel('RMSE [°C]')
    ax2.set_title('Error cuadrático medio por h_ext')
    ax2.grid(True, alpha=0.3, axis='y')
    ax2.annotate(f'Óptimo\n{h_opt} W/m²K', xy=(idx_opt, rmse_vals[idx_opt]),
                 xytext=(idx_opt + 0.5, rmse_vals[idx_opt] + 0.01),
                 arrowprops=dict(arrowstyle='->', color='black'), fontsize=9)

    plt.tight_layout()

    # Guardar en carpeta resultados con timestamp para no sobreescribir
    resultados_dir = os.path.join(_script_dir, 'resultados_calibracion')
    os.makedirs(resultados_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(resultados_dir, f'calibracion_h_ext_{FLUIDO}_{timestamp}.png')
    plt.savefig(out_path, dpi=120, bbox_inches='tight')
    print(f"\nGráfico guardado: {out_path}")
    plt.show()


if __name__ == "__main__":
    main()
