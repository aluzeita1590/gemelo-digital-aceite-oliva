"""
Validación independiente de h_ext usando DS_INT — Experimento de enfriamiento natural

DS_INT nunca participa en el ajuste de calibrar_h_ext.py (que solo usa DS0-DS4).
Este script corre la misma simulación 2D completa (misma ventana, misma condición
inicial interpolada desde la pared) para el h_ext ya calibrado, pero extrae la
temperatura en el nodo interior (r=0, j_INT = N_z//2, igual que modelo.py) y la
compara contra la lectura real de DS_INT — una verificación fuera de muestra, no
una recalibración.

Uso:
    python validar_hext_ds_int.py
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

# ── Misma ventana que calibrar_h_ext.py (experimento del 6-7 oct 2026) ────
T_INICIO = "2026-10-06T20:00:00Z"
T_FIN    = "2026-10-07T16:30:00Z"
FLUIDO   = "agua"

# h_ext a validar (el óptimo encontrado + vecinos para contexto)
H_EXT_VALORES = [8.0, 10.0, 12.0]

FLUIDOS = {
    "aceite": {"rho_0": 912.66, "alpha": 0.0803, "T_0": 20.0, "Cp": 1970.0, "k": 0.17},
    "agua":   {"rho_0": 998.2,  "alpha": 0.0975, "T_0": 20.0, "Cp": 4182.0, "k": 0.598},
}

R  = config.TANQUE_R_M
H  = config.TANQUE_H_M
Nr = config.MODELO_NR
Nz = config.MODELO_NZ
dr = R / (Nr - 1)
dz = H / (Nz - 1)
r  = np.linspace(0, R, Nr)
z  = np.linspace(0, H, Nz)
J_INT = Nz // 2  # igual que modelo.py: _INT_J = Nz // 2
Z_SENSORES_M = [p / 100.0 for p in config.DS_PARED_POSICIONES_CM]

_e_pared = config.TANQUE_PARED_ESPESOR_M
_k_pared = config.TANQUE_PARED_K

INFLUX_TOKEN  = os.environ.get("INFLUX_TOKEN", "")
INFLUX_URL    = os.environ.get("INFLUX_URL_OVERRIDE", config.INFLUX_URL)
INFLUX_ORG    = config.INFLUX_ORG
INFLUX_BUCKET = config.INFLUX_BUCKET

if not INFLUX_TOKEN:
    raise RuntimeError("INFLUX_TOKEN no definido. Revisar archivo .env")

client    = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
query_api = client.query_api()


def leer_sensor(sensor_tag):
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


def leer_datos():
    print("Leyendo datos de InfluxDB...")
    ts_ref = None
    T_pared = []
    for i in range(5):
        ts, vs = leer_sensor(f"DS{i}")
        if ts_ref is None:
            ts_ref = ts
        f = interp1d(ts - ts[0], vs, bounds_error=False, fill_value=(vs[0], vs[-1]))
        T_pared.append(f(ts_ref - ts_ref[0]))
        print(f"  DS{i}: {len(ts)} puntos")

    ts_amb, vs_amb = leer_sensor("DS_AMB")
    print(f"  DS_AMB: {len(ts_amb)} puntos")
    t_rel = ts_ref - ts_ref[0]
    f_amb = interp1d(ts_amb - ts_amb[0], vs_amb, bounds_error=False,
                      fill_value=(vs_amb[0], vs_amb[-1]))
    T_amb_arr = f_amb(t_rel)

    ts_int, vs_int = leer_sensor("DS_INT")
    print(f"  DS_INT: {len(ts_int)} puntos")
    f_int = interp1d(ts_int - ts_int[0], vs_int, bounds_error=False,
                      fill_value=(vs_int[0], vs_int[-1]))
    T_int_arr = f_int(t_rel)

    return t_rel, np.array(T_pared), T_amb_arr, T_int_arr


def simular(T_inicial_pared, t_exp, T_amb_arr, h_ext_val):
    props = FLUIDOS[FLUIDO]
    rho_0, alpha, T_0, Cp, k = props["rho_0"], props["alpha"], props["T_0"], props["Cp"], props["k"]

    rho_min = rho_0 - alpha * (40.0 - T_0)
    alpha_t = k / (rho_min * Cp)
    dt_max  = 0.25 / (alpha_t * (1/dr**2 + 1/dz**2))
    dt      = min(dt_max * 0.8, 30.0)

    U_ext = 1.0 / (_e_pared / _k_pared + 1.0 / h_ext_val)

    f_ic = interp1d(Z_SENSORES_M, T_inicial_pared, kind='linear',
                     fill_value='extrapolate', bounds_error=False)
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

    T_int_sim = np.zeros(len(t_exp))
    T_int_sim[0] = T[0, J_INT]
    t_sim = 0.0
    for idx in range(1, len(t_exp)):
        while t_sim < t_exp[idx]:
            T = paso(T, float(f_amb(t_sim)))
            t_sim += dt
        T_int_sim[idx] = T[0, J_INT]

    return T_int_sim


def main():
    t_exp, T_pared, T_amb_arr, T_int_arr = leer_datos()
    t_min = t_exp / 60.0
    T_inicial_pared = T_pared[:, 0]

    print(f"\nExperimento: {len(t_exp)} puntos | duración {t_exp[-1]/3600:.1f} h")
    print(f"Nodo interior simulado: j_INT={J_INT}  (z={z[J_INT]*100:.1f} cm)")
    print(f"DS_INT inicial: {T_int_arr[0]:.2f} °C | DS_INT final: {T_int_arr[-1]:.2f} °C\n")

    resultados = {}
    for h in H_EXT_VALORES:
        print(f"Simulando interior con h_ext = {h:5.1f} W/(m²·°C) ...", end=" ", flush=True)
        T_sim = simular(T_inicial_pared, t_exp, T_amb_arr, h)
        rmse = np.sqrt(np.mean((T_sim - T_int_arr) ** 2))
        resultados[h] = {"T_sim": T_sim, "rmse": rmse}
        print(f"RMSE (vs DS_INT) = {rmse:.4f} °C")

    h_mejor = min(resultados, key=lambda h: resultados[h]["rmse"])
    print(f"\nMejor ajuste contra DS_INT (fuera de muestra): h_ext={h_mejor} "
          f"con RMSE={resultados[h_mejor]['rmse']:.4f} °C")

    fig, ax = plt.subplots(figsize=(9, 5.5))
    ax.plot(t_min, T_int_arr, 'k-', linewidth=2.5, label='DS_INT (medido, no usado en el ajuste)')
    colores = plt.cm.plasma(np.linspace(0.2, 0.85, len(H_EXT_VALORES)))
    for (h, res), color in zip(resultados.items(), colores):
        lw = 2.5 if h == h_mejor else 1.3
        marker = ' ★' if h == h_mejor else ''
        ax.plot(t_min, res["T_sim"], color=color, linewidth=lw,
                 label=f"Modelo h_ext={h} (RMSE={res['rmse']:.3f}°C){marker}")
    ax.set_xlabel('Tiempo [min]')
    ax.set_ylabel('T [°C]')
    ax.set_title('Validación independiente: DS_INT vs. modelo (nodo interior)')
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    plt.tight_layout()

    out_dir = os.path.join(_script_dir, 'resultados_calibracion')
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(out_dir, f'validacion_hext_ds_int_{ts}.png')
    plt.savefig(out_path, dpi=120, bbox_inches='tight')
    print(f"\nGráfico guardado: {out_path}")


if __name__ == "__main__":
    main()
