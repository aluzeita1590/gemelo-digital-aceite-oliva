"""
Optimización de alpha_K — Búsqueda del valor óptimo de ganancia de asimilación

Motivación: la comparación de las 4 variantes fijas (α_K = 0, 0.2, 0.6, 0.8) en
analisis_cap5.py mostró que la ganancia baja (0.2) supera a la nominal (0.6) usada
en producción. Este script barre α_K de forma continua para encontrar el óptimo
real, en vez de limitarse a esas 4 variantes.

Procedimiento:
  1. Ajustar T_INICIO y T_FIN a la ventana de interés (por defecto, la misma
     ventana de operación continua usada en analisis_cap5.py).
  2. Correr en gemelo5 (acceso local a InfluxDB) o de forma remota con
     INFLUX_URL_OVERRIDE=http://<ip_gemelo5>:8086 python optimizar_alpha_k.py
  3. El script lee DS0-DS4, DS_AMB y DS_INT, reproduce fuera de línea la física +
     asimilación de modelo.py (paso_tiempo + actualizar_con_sensores) para un
     barrido de α_K, calcula el RMSE contra DS_INT para cada uno, refina alrededor
     del mejor valor encontrado, y reporta el óptimo.
  4. Actualizar MODELO_ALPHA_K en config.py con el valor resultante (o correr
     primero con otras ventanas para confirmar que el óptimo es estable).
"""

import os
import sys
import numpy as np
import matplotlib.pyplot as plt
from influxdb_client import InfluxDBClient
from scipy.interpolate import interp1d

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
T_INICIO = "2026-08-19T00:00:00Z"
T_FIN    = "2026-08-26T00:00:00Z"
FLUIDO   = config.MODELO_FLUIDO_DEFAULT   # "aceite", consistente con producción

# Barrido grueso, luego se refina ±0.04 en pasos de 0.01 alrededor del óptimo
ALPHA_K_GRUESO = [round(a, 2) for a in np.arange(0.0, 1.001, 0.05)]

OUT_DIR = os.path.join(_script_dir, "resultados_cap5")
os.makedirs(OUT_DIR, exist_ok=True)

# ── Propiedades de fluidos (idénticas a modelo.py) ─────────────────
FLUIDOS = {
    "aceite": {"rho_0": 912.66, "alpha": 0.0803, "T_0": 20.0, "Cp": 1970.0, "k": 0.17},
    "agua":   {"rho_0": 998.2,  "alpha": 0.0975, "T_0": 20.0, "Cp": 4182.0, "k": 0.598},
}
props   = FLUIDOS[FLUIDO]
rho_0   = props["rho_0"]
alpha_d = props["alpha"]   # coef. de densidad (no confundir con alpha_K)
T_0     = props["T_0"]
Cp      = props["Cp"]
k       = props["k"]

# ── Geometría (idéntica a modelo.py) ────────────────────────────────
R  = config.TANQUE_R_M
H  = config.TANQUE_H_M
Nr = config.MODELO_NR
Nz = config.MODELO_NZ
dr = R / (Nr - 1)
dz = H / (Nz - 1)
r  = np.linspace(0, R, Nr)
z  = np.linspace(0, H, Nz)
Z_SENSORES_M = [p / 100.0 for p in config.DS_PARED_POSICIONES_CM]
_INT_J = Nz // 2   # mismo índice que modelo.py

_e_pared = config.TANQUE_PARED_ESPESOR_M
_k_pared = config.TANQUE_PARED_K
h_ext    = config.MODELO_H_EXT
U_ext    = 1.0 / (_e_pared / _k_pared + 1.0 / h_ext)

rho_min = rho_0 - alpha_d * (40.0 - T_0)
alpha_t_max = k / (rho_min * Cp)
dt_max  = 0.25 / (alpha_t_max * (1 / dr**2 + 1 / dz**2))
dt      = min(dt_max * 0.8, 30.0)

# ── InfluxDB ───────────────────────────────────────────────────────
INFLUX_TOKEN  = os.environ.get("INFLUX_TOKEN", "")
INFLUX_URL    = os.environ.get("INFLUX_URL_OVERRIDE", config.INFLUX_URL)
INFLUX_ORG    = config.INFLUX_ORG
INFLUX_BUCKET = config.INFLUX_BUCKET

if not INFLUX_TOKEN:
    raise RuntimeError("INFLUX_TOKEN no definido. Revisar archivo .env")

client    = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
query_api = client.query_api()


def leer_sensor(sensor_tag):
    """(timestamps_s, valores) de un sensor, resampleado a 1 min."""
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
    print("Leyendo sensores de InfluxDB (resampleo 1 min)...")
    ts_ref = None
    T_pared = []
    for i in range(5):
        ts, vs = leer_sensor(f"DS{i}")
        if ts_ref is None:
            ts_ref = ts
        f = interp1d(ts - ts[0], vs, bounds_error=False, fill_value=(vs[0], vs[-1]))
        T_pared.append(f(ts_ref - ts_ref[0]))
        print(f"  DS{i}: {len(ts)} puntos")

    t_rel = ts_ref - ts_ref[0]

    ts_amb, vs_amb = leer_sensor("DS_AMB")
    f_amb = interp1d(ts_amb - ts_amb[0], vs_amb, bounds_error=False,
                      fill_value=(vs_amb[0], vs_amb[-1]))
    T_amb_arr = f_amb(t_rel)
    print(f"  DS_AMB: {len(ts_amb)} puntos")

    ts_int, vs_int = leer_sensor("DS_INT")
    f_int = interp1d(ts_int - ts_int[0], vs_int, bounds_error=False,
                      fill_value=(vs_int[0], vs_int[-1]))
    T_int_arr = f_int(t_rel)
    print(f"  DS_INT: {len(ts_int)} puntos")

    return t_rel, np.array(T_pared), T_amb_arr, T_int_arr


def simular(alpha_K, t_exp, T_pared, T_amb_arr):
    """
    Reproduce fuera de línea paso_tiempo() + actualizar_con_sensores() de
    modelo.py para una ganancia alpha_K fija. Devuelve T_int_sim (N,): valor
    en el nodo (0, _INT_J) en cada instante de t_exp.
    """
    f_amb = interp1d(t_exp, T_amb_arr, bounds_error=False,
                      fill_value=(T_amb_arr[0], T_amb_arr[-1]))

    def paso(T, T_amb_val):
        T_new = T.copy()
        rho = rho_0 - alpha_d * (T - T_0)
        a   = k / (rho * Cp)
        T_new[1:-1, 1:-1] = T[1:-1, 1:-1] + dt * a[1:-1, 1:-1] * (
            (T[2:, 1:-1] - 2*T[1:-1, 1:-1] + T[:-2, 1:-1]) / dr**2 +
            (1 / r[1:-1, np.newaxis]) * (T[2:, 1:-1] - T[:-2, 1:-1]) / (2*dr) +
            (T[1:-1, 2:] - 2*T[1:-1, 1:-1] + T[1:-1, :-2]) / dz**2
        )
        T_new[0, 1:-1] = T[0, 1:-1] + dt * a[0, 1:-1] * (
            2*(T[1, 1:-1] - T[0, 1:-1]) / dr**2 +
            (T[0, 2:] - 2*T[0, 1:-1] + T[0, :-2]) / dz**2
        )
        T_new[-1, :] = (T[-2, :] + dr * (U_ext / k) * T_amb_val) / (1 + dr * U_ext / k)
        T_new[:, 0]  = T_new[:, 1]
        T_new[:, -1] = T_new[:, -2]
        return T_new

    def asimilar(T, temps_wall):
        interp = interp1d(Z_SENSORES_M, temps_wall, kind='linear',
                           fill_value='extrapolate', bounds_error=False)
        lo, hi = min(temps_wall) - 1.0, max(temps_wall) + 1.0
        for j in range(Nz):
            T_interp = float(np.clip(interp(z[j]), lo, hi))
            T[-1, j] = T[-1, j] + alpha_K * (T_interp - T[-1, j])
        return T

    # Condición inicial: perfil de pared interpolado en t=0 (igual criterio que
    # condicion_inicial_dinamica() en modelo.py)
    T_inicial_pared = T_pared[:, 0]
    f_ic = interp1d(Z_SENSORES_M, T_inicial_pared, kind='linear',
                     fill_value='extrapolate', bounds_error=False)
    T = np.zeros((Nr, Nz))
    for j in range(Nz):
        T[:, j] = float(f_ic(z[j]))

    # modelo.py asimila en CADA ciclo de su loop (cada INTERVALO_MODELO_S=10s),
    # no cada 60s: el ciclo%6==0 de modelo.py solo filtra la ESCRITURA a
    # InfluxDB, no la asimilación. Con datos resampleados a 1 min, replicamos
    # eso aplicando ~6 correcciones por minuto (una por cada INTERVALO_MODELO_S
    # dentro del minuto), usando la misma lectura de pared para las 6 porque no
    # hay resolución más fina que 1 min en los datos resampleados.
    correcciones_por_minuto = max(1, round(60.0 / config.INTERVALO_MODELO_S))
    pasos_por_correccion    = max(1, round(config.INTERVALO_MODELO_S / dt))

    T_int_sim = np.zeros(len(t_exp))
    T_int_sim[0] = T[0, _INT_J]

    for idx in range(1, len(t_exp)):
        T_amb_val = float(f_amb(t_exp[idx]))
        for _ in range(correcciones_por_minuto):
            for _ in range(pasos_por_correccion):
                T = paso(T, T_amb_val)
            T = asimilar(T, T_pared[:, idx])
        T_int_sim[idx] = T[0, _INT_J]

    return T_int_sim


def rmse(sim, medido):
    return float(np.sqrt(np.mean((sim - medido) ** 2)))


def main():
    t_exp, T_pared, T_amb_arr, T_int_arr = leer_datos()
    if len(t_exp) < 100:
        print("ERROR: pocos datos en la ventana indicada.")
        return
    print(f"\n{len(t_exp)} puntos (1 min c/u) | {t_exp[-1]/3600:.1f} h de ventana\n")

    print("=" * 60)
    print("BARRIDO GRUESO (paso 0,05)")
    print("=" * 60)
    resultados = {}
    for a in ALPHA_K_GRUESO:
        T_sim = simular(a, t_exp, T_pared, T_amb_arr)
        e = rmse(T_sim, T_int_arr)
        resultados[a] = e
        print(f"  alpha_K = {a:.2f}   RMSE = {e:.4f} °C")

    a_opt_grueso = min(resultados, key=resultados.get)
    print(f"\nÓptimo del barrido grueso: alpha_K={a_opt_grueso:.2f} "
          f"(RMSE={resultados[a_opt_grueso]:.4f} °C)")

    print("\n" + "=" * 60)
    print(f"REFINAMIENTO alrededor de {a_opt_grueso:.2f} (paso 0,01)")
    print("=" * 60)
    finos = sorted(set(
        round(v, 2) for v in np.arange(
            max(0.0, a_opt_grueso - 0.04), min(1.0, a_opt_grueso + 0.041), 0.01
        )
    ) - set(ALPHA_K_GRUESO))
    for a in finos:
        T_sim = simular(a, t_exp, T_pared, T_amb_arr)
        e = rmse(T_sim, T_int_arr)
        resultados[a] = e
        print(f"  alpha_K = {a:.2f}   RMSE = {e:.4f} °C")

    a_opt = min(resultados, key=resultados.get)
    print(f"\n{'=' * 60}\nÓPTIMO FINAL: alpha_K = {a_opt:.2f}  "
          f"(RMSE = {resultados[a_opt]:.4f} °C)\n{'=' * 60}")
    print(f"Actualizar en config.py:  MODELO_ALPHA_K = {a_opt}")

    # ── Gráfico RMSE vs. alpha_K ────────────────────────────────
    alphas_ord = sorted(resultados)
    rmses_ord = [resultados[a] for a in alphas_ord]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(alphas_ord, rmses_ord, 'o-', color='tab:blue', markersize=4)
    ax.axvline(a_opt, color='red', linestyle='--', linewidth=1,
               label=f'Óptimo α_K={a_opt:.2f}')
    ax.axhline(1.0, color='gray', linestyle=':', linewidth=1,
               label='Criterio de aceptación (1,0 °C)')
    ax.set_xlabel(r'Ganancia de asimilación $\alpha_K$')
    ax.set_ylabel('RMSE [°C] — DS_INT vs. modelo')
    ax.set_title('Búsqueda del óptimo de ganancia de asimilación')
    ax.legend()
    ax.grid(alpha=0.3)
    plt.tight_layout()
    out_path = os.path.join(OUT_DIR, "optimizacion_alpha_k.png")
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"\nFigura guardada: {out_path}")


if __name__ == "__main__":
    main()
