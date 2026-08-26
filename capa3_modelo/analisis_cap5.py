"""
Análisis de validación — Capítulo 5 de la tesis (Resultados y Conclusiones)

Procedimiento:
  1. Ajustar T_INICIO y T_FIN a la ventana del experimento de validación
     descrito en cap4 (Sección "Validación del modelo numérico"): llenado
     al ~85%, perturbación térmica ≥3°C entre fondo y superficie, mínimo
     60 min de registro.
  2. Correr en gemelo5 (acceso local a InfluxDB): python analisis_cap5.py
  3. El script imprime en pantalla:
       - RMSE, MAE y R² para las 4 variantes de asimilación (α=0, 0.2, 0.6, 0.8)
         comparadas contra DS_INT, más una tabla LaTeX lista para pegar en
         tab:metricas_error de cap5_resultados.tex.
       - Estadísticas de muestreo efectivo y disponibilidad (Sección
         "Desempeño del sistema de adquisición").
       - Error relativo V_nivel vs V_modelo (para contrastar con el ~7%
         mencionado en el README).
  4. Guarda las figuras en resultados_cap5/ — copiar las que se quieran usar
     a tesis-uach/figuras/ y referenciarlas en cap5_resultados.tex.
"""

import os
import sys
import numpy as np
import matplotlib.pyplot as plt
from influxdb_client import InfluxDBClient

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
# Sin experimento de perturbación controlada disponible: se usa todo el
# período de operación continua registrado (2026-07-08 en adelante), con
# ciclos térmicos diurnos naturales como fuente de gradiente vertical.
# Se pueden sobreescribir con duraciones relativas Flux (ej. "-24h", "-12h")
# o timestamps ISO 8601, vía T_INICIO_OVERRIDE / T_FIN_OVERRIDE:
#   T_INICIO_OVERRIDE=-24h T_FIN_OVERRIDE=now() python analisis_cap5.py
T_INICIO = os.environ.get("T_INICIO_OVERRIDE", "2026-08-19T00:00:00Z")
T_FIN    = os.environ.get("T_FIN_OVERRIDE", "2026-08-26T00:00:00Z")

OUT_DIR = os.path.join(_script_dir, "resultados_cap5")
os.makedirs(OUT_DIR, exist_ok=True)

# ── InfluxDB ───────────────────────────────────────────────────────
# config.INFLUX_URL = "http://localhost:8086" asume que el script corre EN
# gemelo5. Si se ejecuta de forma remota (ej. este análisis desde otra
# máquina en la misma red), definir INFLUX_URL_OVERRIDE, p.ej.:
#   INFLUX_URL_OVERRIDE=http://192.168.1.104:8086 python analisis_cap5.py
INFLUX_TOKEN  = os.environ.get("INFLUX_TOKEN", "")
INFLUX_URL    = os.environ.get("INFLUX_URL_OVERRIDE", config.INFLUX_URL)
INFLUX_ORG    = config.INFLUX_ORG
INFLUX_BUCKET = config.INFLUX_BUCKET

if not INFLUX_TOKEN:
    raise RuntimeError("INFLUX_TOKEN no definido. Revisar archivo .env")

client    = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
query_api = client.query_api()


def _query(flux):
    result = query_api.query(flux)
    return [rec for table in result for rec in table.records]


def leer_validacion():
    """
    Lee measurement 'validacion_interior' (escrito cada 60 s por modelo.py)
    en la ventana [T_INICIO, T_FIN]. Devuelve dict de arrays numpy, uno por
    field, más 't' (timestamps relativos en segundos).
    """
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {T_INICIO}, stop: {T_FIN})
      |> filter(fn: (r) => r._measurement == "validacion_interior")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
      |> sort(columns: ["_time"])
    '''
    records = _query(flux)
    if not records:
        raise RuntimeError(
            "Sin datos en 'validacion_interior' para la ventana indicada. "
            "Verificar T_INICIO/T_FIN y que modelo.service estuvo activo."
        )
    campos = ["T_medida_C", "T_modelo_C", "error_C",
              "T_alta_C", "error_alta_C", "T_baja_C", "error_baja_C",
              "T_libre_C", "error_libre_C",
              "T_a30_C", "error_a30_C", "T_a35_C", "error_a35_C",
              "T_a40_C", "error_a40_C"]
    datos = {c: [] for c in campos}
    t0 = records[0].get_time().timestamp()
    t = []
    for rec in records:
        t.append(rec.get_time().timestamp() - t0)
        for c in campos:
            datos[c].append(rec.values.get(c))
    out = {"t": np.array(t)}
    for c in campos:
        out[c] = np.array(datos[c], dtype=float)
    return out


def metricas(t_modelo, t_medida):
    """
    RMSE, MAE, R² y N del modelo respecto de la medición, descartando pares
    con NaN (campos de variantes que se agregaron a modelo.py después del
    inicio de la ventana no tienen valor en los ciclos previos a su
    despliegue).
    """
    valido = ~np.isnan(t_modelo) & ~np.isnan(t_medida)
    t_modelo, t_medida = t_modelo[valido], t_medida[valido]
    err = t_modelo - t_medida
    rmse = np.sqrt(np.mean(err ** 2))
    mae = np.mean(np.abs(err))
    ss_res = np.sum(err ** 2)
    ss_tot = np.sum((t_medida - np.mean(t_medida)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return rmse, mae, r2, int(valido.sum())


def reportar_metricas(datos):
    variantes = [
        ("Sin asimilación (α=0,00)", "T_libre_C"),
        ("Baja (α=0,20)",            "T_baja_C"),
        ("α=0,30 (exploratoria)",    "T_a30_C"),
        ("α=0,35 (exploratoria)",    "T_a35_C"),
        ("α=0,40 (exploratoria)",    "T_a40_C"),
        ("Nominal (α=0,60)",         "T_modelo_C"),
        ("Alta (α=0,80)",            "T_alta_C"),
    ]
    print("\n" + "=" * 60)
    print("MÉTRICAS DE VALIDACIÓN — DS_INT vs. modelo")
    print("=" * 60)
    filas_latex = []
    for nombre, campo in variantes:
        rmse, mae, r2, n = metricas(datos[campo], datos["T_medida_C"])
        print(f"  {nombre:28s}  RMSE={rmse:.3f} °C  MAE={mae:.3f} °C  "
              f"R²={r2:.4f}  (N={n})")
        filas_latex.append(
            f"    {nombre} & {rmse:.3f} & {mae:.3f} & {r2:.4f} \\\\"
        )
    print(f"\n  N total de la ventana = {len(datos['t'])} muestras "
          f"({datos['t'][-1]/60:.1f} min); N por variante puede ser menor si "
          f"esa variante se agregó a modelo.py después del inicio de la ventana.")
    print(f"  Criterio de aceptación: RMSE < 1,0 °C")

    print("\n--- Tabla LaTeX (pegar en tab:metricas_error, cap5_resultados.tex) ---\n")
    print(r"\begin{table}[H]")
    print(r"  \centering")
    print(r"  \caption{Métricas de error del modelo en el prototipo de 20~L, "
          r"por variante de ganancia de asimilación.}")
    print(r"  \label{tab:metricas_error}")
    print(r"  \begin{tabular}{lccc}")
    print(r"    \toprule")
    print(r"    \textbf{Variante} & \textbf{RMSE (°C)} & \textbf{MAE (°C)} & \textbf{$R^2$} \\")
    print(r"    \midrule")
    for fila in filas_latex:
        print(fila)
    print(r"    \bottomrule")
    print(r"  \end{tabular}")
    print(r"\end{table}")


def graficar_validacion(datos):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 7), sharex=True)
    t_min = datos["t"] / 60.0

    ax1.plot(t_min, datos["T_medida_C"], "k-", linewidth=2, label="DS_INT (medido)")
    ax1.plot(t_min, datos["T_libre_C"], "--", label="Sin asimilación (α=0,00)")
    ax1.plot(t_min, datos["T_baja_C"], "--", label="Baja (α=0,20)")
    ax1.plot(t_min, datos["T_modelo_C"], "-", label="Nominal (α=0,60)")
    ax1.plot(t_min, datos["T_alta_C"], "--", label="Alta (α=0,80)")
    ax1.set_ylabel("Temperatura [°C]")
    ax1.set_title("Validación interior — DS_INT vs. modelo")
    ax1.legend(fontsize=8, loc="best")
    ax1.grid(alpha=0.3)

    ax2.plot(t_min, datos["error_libre_C"], "--", label="Sin asimilación")
    ax2.plot(t_min, datos["error_baja_C"], "--", label="Baja")
    ax2.plot(t_min, datos["error_C"], "-", label="Nominal")
    ax2.plot(t_min, datos["error_alta_C"], "--", label="Alta")
    ax2.axhline(0, color="gray", linewidth=0.8)
    ax2.set_xlabel("Tiempo [min]")
    ax2.set_ylabel("Error (modelo − medido) [°C]")
    ax2.legend(fontsize=8, loc="best")
    ax2.grid(alpha=0.3)

    plt.tight_layout()
    out_path = os.path.join(OUT_DIR, "validacion_interior.png")
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"\nFigura guardada: {out_path}")


def leer_pared():
    """
    Lee DS0-DS4, DS_AMB y DS_SUP en la ventana, para el perfil de pared.
    Submuestrea a 5 min cuando la ventana supera 1 día, para no traer
    millones de puntos crudos en ventanas largas de operación continua.
    """
    # T_INICIO/T_FIN pueden ser timestamps ISO 8601 o duraciones relativas
    # Flux (ej. "-24h", "now()") — solo se puede estimar la ventana con ISO.
    try:
        ventana_s = (
            __import__("datetime").datetime.fromisoformat(T_FIN.replace("Z", "+00:00"))
            - __import__("datetime").datetime.fromisoformat(T_INICIO.replace("Z", "+00:00"))
        ).total_seconds()
    except ValueError:
        ventana_s = 0   # duración relativa: se asume ventana corta, sin agregar
    agregar = ventana_s > 86400
    sensores = ["DS0", "DS1", "DS2", "DS3", "DS4", "DS_AMB", "DS_SUP"]
    salida = {}
    for s in sensores:
        agg_step = (
            '|> aggregateWindow(every: 5m, fn: mean, createEmpty: false)'
            if agregar else ''
        )
        flux = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: {T_INICIO}, stop: {T_FIN})
          |> filter(fn: (r) => r._measurement == "temperatura")
          |> filter(fn: (r) => r.sensor == "{s}")
          |> filter(fn: (r) => r._field == "valor")
          {agg_step}
          |> sort(columns: ["_time"])
        '''
        records = _query(flux)
        if not records:
            continue
        t0 = records[0].get_time().timestamp()
        t = np.array([r.get_time().timestamp() - t0 for r in records])
        v = np.array([float(r.get_value()) for r in records])
        salida[s] = (t, v)
    return salida


def graficar_pared(datos_pared):
    if not datos_pared:
        print("Sin datos de pared en la ventana — se omite figura de perfil.")
        return
    fig, ax = plt.subplots(figsize=(9, 5))
    for s, (t, v) in datos_pared.items():
        ax.plot(t / 60.0, v, label=s)
    ax.set_xlabel("Tiempo [min]")
    ax.set_ylabel("Temperatura [°C]")
    ax.set_title("Temperaturas medidas durante el experimento de validación")
    ax.legend(fontsize=8, loc="best")
    ax.grid(alpha=0.3)
    plt.tight_layout()
    out_path = os.path.join(OUT_DIR, "temperaturas_pared.png")
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"Figura guardada: {out_path}")


def evaluar_muestreo():
    """
    Frecuencia de muestreo efectiva y disponibilidad, a partir de los
    timestamps crudos de DS0 (measurement 'temperatura') en la ventana.
    Nominal: sensor.py publica cada INTERVALO_SENSOR_S = 10 s.
    """
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {T_INICIO}, stop: {T_FIN})
      |> filter(fn: (r) => r._measurement == "temperatura")
      |> filter(fn: (r) => r.sensor == "DS0")
      |> filter(fn: (r) => r._field == "valor")
      |> sort(columns: ["_time"])
    '''
    records = _query(flux)
    if len(records) < 2:
        print("\nMuy pocos puntos para evaluar muestreo en esta ventana.")
        return
    ts = np.array([r.get_time().timestamp() for r in records])
    dt = np.diff(ts)
    nominal = config.INTERVALO_SENSOR_S
    duracion_s = ts[-1] - ts[0]
    esperados = duracion_s / nominal
    gaps = dt[dt > 2 * nominal]

    print("\n" + "=" * 60)
    print("DESEMPEÑO DEL SISTEMA DE ADQUISICIÓN (basado en DS0)")
    print("=" * 60)
    print(f"  Intervalo nominal configurado : {nominal} s")
    print(f"  Intervalo medio efectivo      : {np.mean(dt):.2f} s "
          f"(desv. std {np.std(dt):.2f} s)")
    print(f"  Muestras recibidas / esperadas: {len(records)} / {esperados:.0f} "
          f"({100*len(records)/esperados:.1f}% disponibilidad)")
    print(f"  Interrupciones (>2x nominal)  : {len(gaps)}"
          + (f", máxima {gaps.max():.0f} s" if len(gaps) else ""))


def leer_volumen_masa():
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {T_INICIO}, stop: {T_FIN})
      |> filter(fn: (r) => r._measurement == "volumen_masa")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
      |> sort(columns: ["_time"])
    '''
    records = _query(flux)
    if not records:
        print("\nSin datos en 'volumen_masa' para esta ventana.")
        return
    v_niv = np.array([r.values.get("V_nivel_L") for r in records], dtype=float)
    v_mod = np.array([r.values.get("V_modelo_L") for r in records], dtype=float)
    mask = ~np.isnan(v_niv) & ~np.isnan(v_mod) & (v_niv > 0)
    err_rel = np.abs(v_mod[mask] - v_niv[mask]) / v_niv[mask] * 100

    print("\n" + "=" * 60)
    print("VOLUMEN: V_nivel (HC-SR04) vs. V_modelo (integración ρ(r,z))")
    print("=" * 60)
    print(f"  Error relativo medio : {np.mean(err_rel):.1f} %")
    print(f"  Error relativo máximo: {np.max(err_rel):.1f} %")
    print("  (contrastar con el ~7% reportado en README.md tras la corrección "
          "geométrica)")


if __name__ == "__main__":
    print(f"Ventana de análisis: {T_INICIO} → {T_FIN}")

    datos_val = leer_validacion()
    reportar_metricas(datos_val)
    graficar_validacion(datos_val)

    datos_pared = leer_pared()
    graficar_pared(datos_pared)

    evaluar_muestreo()
    leer_volumen_masa()

    print(f"\nListo. Figuras en: {OUT_DIR}")
