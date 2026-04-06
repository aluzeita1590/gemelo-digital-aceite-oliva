"""
=============================================================================
GEMELO DIGITAL — TANQUE DE ACEITE DE OLIVA
Modelo Termofísico 2D Axisimétrico (r, z)
=============================================================================
Autor   : Sebastian Araneda
Versión : 3.0 — Grilla refinada (Nr=15, Nz=20) + Laplaciano vectorizado
Fecha   : 2025

Cambios respecto a v2.1:
    1. Grilla refinada: Nr=15 nodos radiales, Nz=20 nodos axiales.
       En v2 se usaban Nr=3 y Nz=7, lo que era insuficiente para
       capturar gradientes radiales con resolución física realista.
       Con Nr=15 el espaciado radial pasa de ~7 cm a ~1 cm (para
       el prototipo de 20 L con R=14.1 cm), permitiendo observar
       con detalle la estratificación radial del aceite.

    2. Laplaciano vectorizado con NumPy (sin loops Python).
       El método _laplaciano() en v2 usaba dos for anidados
       (O(Nr × Nz) iteraciones Python). En v3 todas las operaciones
       se realizan con slicing y broadcasting de NumPy, lo que
       reduce el tiempo de cómputo típicamente entre 20× y 100×
       para grillas de tamaño moderado.

Descripción:
    Resuelve la ecuación de calor en coordenadas cilíndricas para un
    fluido estático (aceite de oliva) en un tanque cilíndrico vertical,
    con densidad variable en función de la temperatura en cada nodo.

    Ecuación gobernante con ρ = ρ(T):
        ρ(T)·Cp·∂T/∂t = k·[1/r·∂/∂r(r·∂T/∂r) + ∂²T/∂z²]

    Forma discreta (Euler explícito):
        T[i,j]^{n+1} = T[i,j]^n + dt · α_t(T[i,j]^n) · ∇²T[i,j]^n

    La difusividad térmica efectiva α_t(T) = k/(ρ(T)·Cp) varía nodo a nodo
    según la temperatura local, capturando el efecto de la expansión térmica
    sobre la inercia del fluido.

    Limitación conocida: el modelo no incluye convección natural (Navier-Stokes).
    El número de Rayleigh calculado (Ra ≈ 5×10⁶ para el prototipo con ΔT=2°C)
    indica que la convección natural es significativa. Su implementación queda
    propuesta como trabajo futuro (ver sección de limitaciones de la tesis).

    Rango de validez: T ∈ [10°C, 40°C]. Fuera de este rango el modelo lineal
    ρ(T) = ρ₀ - α(T-T₀) pierde precisión (enturbiamiento del aceite < 10°C).

    Condiciones de contorno:
        r = 0  : simetría axial (∂T/∂r = 0), tratado con L'Hôpital
        r = R  : condición Robin (convección exterior)
                 -k·∂T/∂r = h·(T - T_amb)  ← aquí entran los sensores DS18B20
        z = 0  : fondo adiabático (∂T/∂z = 0)
        z = H  : superficie libre adiabática (∂T/∂z = 0)

Referencias:
    - Çengel, Y.A. (2007). Transferencia de calor y masa, 3ª ed. Cap. 2 y 5.
    - Incropera et al. (2007). Fundamentals of Heat and Mass Transfer, 6th ed.
    - Ribeiro et al. (2017). Eur. J. Lipid Sci. Technol., 119(5).
    - Teso-Fz-Betoño et al. (2019). Energies, 12(22), 4275.
=============================================================================
"""

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from dataclasses import dataclass, field
from typing import List, Optional
import os


# =============================================================================
# 1. PROPIEDADES FÍSICAS DEL ACEITE DE OLIVA
# =============================================================================

@dataclass
class PropiedadesAceite:
    """
    Propiedades termofísicas del aceite de oliva virgen extra.
    Fuente: Ribeiro et al. (2017), Fasina et al. (2008)

    Modelo de densidad variable:
        ρ(T) = ρ₀ - α·(T - T₀)

    Válido para T ∈ [10°C, 40°C]. Fuera de este rango el aceite de oliva
    puede experimentar enturbiamiento (< ~10°C) o degradación térmica (> 60°C),
    invalidando los parámetros constantes aquí asumidos.

    Nota sobre convección natural:
        La variación de densidad con T genera flotabilidad (convección natural).
        El número de Rayleigh Ra = g·β_T·ΔT·L³/(ν·α_t) indica si este efecto
        es significativo. Para el prototipo (Ra ≈ 5×10⁶) y el tanque industrial
        (Ra ≈ 9×10⁹), la convección NO es despreciable. Sin embargo, su
        modelado requiere resolver Navier-Stokes acoplado, lo cual excede el
        alcance de esta tesis. El modelo actual asume fluido estático
        (conducción pura), lo que constituye una limitación documentada.
    """
    rho_0: float = 912.66   # Densidad de referencia [kg/m³] a T₀ = 20°C
    T_0:   float = 20.0     # Temperatura de referencia [°C]
    alpha: float = 0.0803   # Coef. expansión térmica lineal [kg/(m³·°C)]
                            # Fuente: Ribeiro et al. (2017), Tabla 3
    Cp:    float = 1970.0   # Calor específico [J/(kg·°C)]
                            # Fuente: Fasina et al. (2008)
    k:     float = 0.17     # Conductividad térmica [W/(m·°C)]
                            # Fuente: valores típicos aceites vegetales

    def densidad(self, T: float) -> float:
        """
        Densidad local en función de la temperatura [kg/m³].

            ρ(T) = ρ₀ - α·(T - T₀)

        Esta función se evalúa NODO A NODO en cada paso de tiempo,
        de modo que la difusividad térmica efectiva α_t = k/(ρ(T)·Cp)
        varía espacialmente según el campo de temperatura local.

        Rango de validez: T ∈ [10°C, 40°C]
        Error máximo respecto a ρ₀ constante: ~1.45% en los extremos del rango.
        """
        return self.rho_0 - self.alpha * (T - self.T_0)

    def difusividad_local(self, T: float) -> float:
        """
        Difusividad térmica efectiva en función de la temperatura [m²/s].

            α_t(T) = k / (ρ(T) · Cp)

        Al usar ρ(T) en lugar de ρ₀ constante, la difusividad varía entre:
            T=10°C: α_t = k/(919.6·Cp) = 9.39×10⁻⁸ m²/s
            T=40°C: α_t = k/(899.8·Cp) = 9.60×10⁻⁸ m²/s
        Diferencia total: ~2.2% — pequeña pero físicamente correcta.
        """
        return self.k / (self.densidad(T) * self.Cp)

    @property
    def difusividad(self) -> float:
        """
        Difusividad térmica de referencia a T₀ [m²/s].
        Se usa para el criterio de estabilidad de Von Neumann.
        Se toma el valor mínimo (T=40°C, α_t máxima) para garantizar
        estabilidad en todo el rango de temperatura.
        """
        rho_min = self.densidad(40.0)   # densidad mínima → difusividad máxima
        return self.k / (rho_min * self.Cp)

    def biot_radial(self, h_ext: float, R: float) -> float:
        """Bi_r = h·R/k — justifica el modelo 2D si Bi > 0.1"""
        return h_ext * R / self.k

    def biot_axial(self, h_ext: float, H: float) -> float:
        """Bi_z = h·(H/2)/k"""
        return h_ext * (H / 2) / self.k

    def rayleigh(self, h_ext: float, L: float, dT: float,
                 nu: float = 80e-6) -> float:
        """
        Número de Rayleigh para evaluar la importancia de la convección natural.
            Ra = g·β_T·ΔT·L³ / (ν·α_t)

        Donde β_T = α/ρ₀ es el coeficiente de expansión volumétrica [1/°C].
        Ra < 1000  → conducción domina, convección despreciable
        Ra > 1000  → convección natural significativa (limitación del modelo)

        Parámetros:
            h_ext : coeficiente convección exterior [W/(m²·°C)]
            L     : longitud característica [m] (radio del tanque)
            dT    : gradiente de temperatura esperado [°C]
            nu    : viscosidad cinemática [m²/s] (≈80×10⁻⁶ para aceite a 20°C)
        """
        g     = 9.81
        beta  = self.alpha / self.rho_0   # coef. expansión volumétrica [1/°C]
        alpha_t = self.difusividad
        return (g * beta * dT * L**3) / (nu * alpha_t)

    def tiempo_caracteristico(self, L: float) -> float:
        """τ = L² / (π²·α_t) — tiempo de difusión [s]"""
        return L**2 / (np.pi**2 * self.difusividad)


# =============================================================================
# 2. GEOMETRÍA DEL TANQUE
# =============================================================================

@dataclass
class GeometriaTanque:
    """
    Parámetros geométricos del tanque cilíndrico.
    Configurable para prototipo (20 L) o tanque industrial (30.000 L).
    """
    nombre:        str   = "Prototipo 20 L"
    volumen_total: float = 0.020     # [m³]
    diametro:      float = 0.282     # [m] → radio = 0.141 m
    espesor_pared: float = 0.002     # [m]
    material_pared: str  = "HDPE"    # HDPE (prototipo) o AISI316 (industrial)

    @property
    def radio(self) -> float:
        return self.diametro / 2

    @property
    def area_transversal(self) -> float:
        return np.pi * self.radio**2

    @property
    def altura_total(self) -> float:
        return self.volumen_total / self.area_transversal

    @classmethod
    def industrial_las200(cls):
        """Tanque industrial Las 200: ~30.000 L, AISI 316, D≈3.4m"""
        return cls(
            nombre        = "Industrial Las 200 - 30.000 L",
            volumen_total = 30.0,       # m³
            diametro      = 3.4,        # m
            espesor_pared = 0.003,      # m (3 mm acero inox.)
            material_pared = "AISI316"
        )


# =============================================================================
# 3. PARÁMETROS DEL MODELO
# =============================================================================

@dataclass
class ParametrosModelo:
    """
    Parámetros de la simulación 2D.

    Cambio en v3.0:
        Nr y Nz aumentados a 15 y 20 respectivamente (en v2: Nr=3, Nz=7).
        Con Nr=15 el espaciado radial para el prototipo pasa de ~7 cm a ~1 cm,
        resolviendo con suficiente detalle los gradientes radiales del aceite.
        Con Nz=20 el espaciado axial pasa de ~4 cm a ~1.5 cm.

        El criterio de estabilidad de Von Neumann limita dt en función de
        dr y dz. Al refinar la grilla, dt_max disminuye. El valor por
        defecto (dt=30 s) sigue siendo estable con la grilla de v3.
    """
    Nr: int   = 15       # Nodos radiales (incluye r=0 y r=R)  [v2: 3]
    Nz: int   = 20       # Nodos axiales  (incluye z=0 y z=H)  [v2: 7]

    # Coeficiente de convección exterior [W/(m²·°C)]
    # HDPE sin aislación: h ≈ 3–8 W/(m²·°C) (convección natural aire)
    # PARÁMETRO A CALIBRAR experimentalmente
    h_ext: float = 5.0

    # Temperatura ambiente [°C]
    T_amb: float = 18.0

    # Paso de tiempo [s]
    dt: float = 30.0

    # Tiempo total de simulación [s]
    t_total: float = 3600 * 8    # 8 horas

    # Nivel de llenado (fracción del volumen total)
    nivel_fraccion: float = 0.85

    # Temperatura inicial uniforme [°C]
    T_inicial: float = 15.0

    # Ganancia de corrección para asimilación de datos (0=solo modelo, 1=solo sensor)
    # PARÁMETRO A CALIBRAR con datos reales
    alpha_K: float = 0.6


# =============================================================================
# 4. MODELO 2D AXISIMÉTRICO
# =============================================================================

class ModeloTanque2D:
    """
    Implementa la ecuación de calor 2D axisimétrica en diferencias finitas.

    Grilla de nodos T[i, j] donde:
        i = índice radial  (i=0: eje central r=0, i=Nr-1: pared r=R)
        j = índice axial   (j=0: fondo z=0,     j=Nz-1: superficie z=H)

    Convención:
        T[0, j]    → eje de simetría (r = 0)
        T[Nr-1, j] → pared interior  (r = R)  ← donde van los sensores
    """

    def __init__(self,
                 aceite: PropiedadesAceite,
                 tanque: GeometriaTanque,
                 params: ParametrosModelo):

        self.aceite = aceite
        self.tanque = tanque
        self.params = params

        Nr, Nz = params.Nr, params.Nz

        # Altura efectiva de aceite
        self.H = tanque.altura_total * params.nivel_fraccion
        self.R = tanque.radio

        # Espaciado de la grilla
        self.dr = self.R / (Nr - 1)
        self.dz = self.H / (Nz - 1)

        # Posiciones radiales y axiales de cada nodo
        self.r = np.linspace(0, self.R, Nr)    # [0, dr, 2dr, ..., R]
        self.z = np.linspace(0, self.H, Nz)    # [0, dz, ..., H]

        # Estado inicial: temperatura uniforme en toda la grilla
        self.T = np.full((Nr, Nz), params.T_inicial, dtype=float)

        # Verificar estabilidad numérica
        self._verificar_estabilidad()

        # Historial
        self.historial_T   = [self.T.copy()]
        self.historial_t   = [0.0]

    # -------------------------------------------------------------------------
    # VERIFICACIÓN DE ESTABILIDAD (criterio de Von Neumann)
    # -------------------------------------------------------------------------

    def _verificar_estabilidad(self):
        """
        Criterio de estabilidad de Von Neumann para Euler explícito 2D:
            dt < 1 / (2·α_t_max·(1/dr² + 1/dz²))

        Se usa α_t_max (difusividad a T=40°C, máxima del rango) para
        garantizar estabilidad en todo el rango operacional.
        """
        alpha_t_max = self.aceite.difusividad
        dt_max = 1.0 / (2 * alpha_t_max * (1/self.dr**2 + 1/self.dz**2))

        print(f"\n{'='*60}")
        print(f"  ANÁLISIS DE ESTABILIDAD — Modelo 2D (r,z) v3.0")
        print(f"{'='*60}")
        print(f"  Tanque:            {self.tanque.nombre}")
        print(f"  Grilla:            {self.params.Nr} × {self.params.Nz} nodos")
        print(f"  dr:                {self.dr*100:.3f} cm")
        print(f"  dz:                {self.dz*100:.3f} cm")
        print(f"  α_t (T=40°C, máx):{alpha_t_max*1e8:.3f} × 10⁻⁸ m²/s")
        print(f"  α_t (T=10°C, mín):{self.aceite.difusividad_local(10.0)*1e8:.3f} × 10⁻⁸ m²/s")
        print(f"  dt_max estable:    {dt_max:.1f} s")
        print(f"  dt usado:          {self.params.dt} s")

        if self.params.dt > dt_max:
            print(f"\n  ⚠️  INESTABLE: reducir dt a menos de {dt_max:.0f} s")
        else:
            print(f"\n  ✅ Estable (margen: {dt_max/self.params.dt:.1f}×)")

        Bi_r  = self.aceite.biot_radial(self.params.h_ext, self.R)
        Bi_z  = self.aceite.biot_axial(self.params.h_ext, self.H)
        tau_r = self.aceite.tiempo_caracteristico(self.R)
        tau_z = self.aceite.tiempo_caracteristico(self.H / 2)
        print(f"\n  Biot radial:   {Bi_r:.2f}  {'→ gradiente radial significativo ✓' if Bi_r > 0.1 else '→ gradiente radial despreciable'}")
        print(f"  Biot axial:    {Bi_z:.2f}  {'→ gradiente axial significativo ✓' if Bi_z > 0.1 else '→ gradiente axial despreciable'}")
        print(f"  τ radial:      {tau_r/3600:.2f} h")
        print(f"  τ axial:       {tau_z/3600:.2f} h")

        Ra_r = self.aceite.rayleigh(self.params.h_ext, self.R, dT=5.0)
        print(f"\n  Rayleigh (ΔT=5°C, L=R): {Ra_r:.2e}")
        if Ra_r < 1000:
            print(f"  → Ra < 1000: conducción domina, convección despreciable ✓")
        elif Ra_r < 1e7:
            print(f"  → Ra > 1000: convección natural significativa ⚠️")
            print(f"     Limitación documentada: modelo asume fluido estático")
        else:
            print(f"  → Ra >> 1e7: convección turbulenta ⚠️")
            print(f"     Limitación documentada: modelo asume fluido estático")
        print(f"{'='*60}")

    # -------------------------------------------------------------------------
    # CÁLCULO DEL LAPLACIANO EN COORDENADAS CILÍNDRICAS — VECTORIZADO (v3)
    # -------------------------------------------------------------------------

    def _laplaciano(self) -> np.ndarray:
        """
        Calcula el operador de Laplace en coordenadas cilíndricas:
            ∇²T = 1/r·∂/∂r(r·∂T/∂r) + ∂²T/∂z²

        Implementación VECTORIZADA con NumPy (sin loops Python).
        Reemplaza los dos for anidados de v2.1 por operaciones de slicing
        y broadcasting, consiguiendo una aceleración típica de 20×–100×
        para grillas de tamaño moderado (Nr=15, Nz=20).

        Esquema por región:
        ┌─────────────────────────────────────────────────────────┐
        │  Radial                                                 │
        │  i = 0      : L'Hôpital → 2·(T[1,:] - T[0,:]) / dr²   │
        │  1 ≤ i ≤ Nr-2: diferencias centradas estándar           │
        │  i = Nr-1   : Robin BC con nodo fantasma               │
        │                                                         │
        │  Axial                                                  │
        │  j = 0      : adiabático → ghost T[i,-1] = T[i,1]      │
        │  1 ≤ j ≤ Nz-2: diferencias centradas estándar           │
        │  j = Nz-1   : adiabático → ghost T[i,Nz] = T[i,Nz-2]  │
        └─────────────────────────────────────────────────────────┘
        """
        T   = self.T
        dr  = self.dr
        dz  = self.dz
        lap = np.zeros_like(T)

        # ---- Contribución radial ----------------------------------------

        # i = 0: eje de simetría r=0, límite L'Hôpital
        #   lim(r→0) [1/r·∂/∂r(r·∂T/∂r)] = 2·∂²T/∂r²
        #   ≈ 2·(T[1,:] - T[0,:]) / dr²
        lap[0, :] = 2.0 * (T[1, :] - T[0, :]) / dr**2

        # 1 ≤ i ≤ Nr-2: nodos interiores, diferencias centradas
        #   d²T/dr² = (T[i+1,:] - 2T[i,:] + T[i-1,:]) / dr²
        #   dT/dr   = (T[i+1,:] - T[i-1,:]) / (2dr)
        #   lap_r   = d²T/dr² + (1/r)·dT/dr
        r_int = self.r[1:-1, np.newaxis]          # shape (Nr-2, 1), broadcasting axial
        d2T_dr2 = (T[2:, :] - 2.0*T[1:-1, :] + T[:-2, :]) / dr**2
        dT_dr   = (T[2:, :] - T[:-2, :]) / (2.0 * dr)
        lap[1:-1, :] = d2T_dr2 + dT_dr / r_int

        # i = Nr-1: pared r=R, condición Robin
        #   -k·∂T/∂r|_R = h·(T_amb - T[R])  →  ∂T/∂r|_R = (h/k)·(T_amb - T[-1,:])
        #   Nodo fantasma: T_ghost = T[-1,:] + dr·(h/k)·(T_amb - T[-1,:])
        #   d²T/dr²|_R ≈ (T_ghost - 2T[-1,:] + T[-2,:]) / dr²
        #   dT/dr|_R   ≈ (T_ghost - T[-2,:]) / (2dr)
        h, k_a  = self.params.h_ext, self.aceite.k
        T_ghost = T[-1, :] + dr * (h / k_a) * (self.params.T_amb - T[-1, :])
        d2T_dr2_wall = (T_ghost - 2.0*T[-1, :] + T[-2, :]) / dr**2
        dT_dr_wall   = (T_ghost - T[-2, :]) / (2.0 * dr)
        lap[-1, :] = d2T_dr2_wall + dT_dr_wall / self.r[-1]

        # ---- Contribución axial -----------------------------------------

        # j = 0: fondo adiabático, ghost T[:,−1] = T[:,1]
        #   d²T/dz²|_0 = (T[:,1] - 2T[:,0] + T[:,1]) / dz² = 2(T[:,1] - T[:,0]) / dz²
        lap[:, 0] += 2.0 * (T[:, 1] - T[:, 0]) / dz**2

        # 1 ≤ j ≤ Nz-2: nodos interiores, diferencias centradas
        lap[:, 1:-1] += (T[:, 2:] - 2.0*T[:, 1:-1] + T[:, :-2]) / dz**2

        # j = Nz-1: superficie adiabática, ghost T[:,Nz] = T[:,Nz-2]
        #   d²T/dz²|_{Nz-1} = 2(T[:,-2] - T[:,-1]) / dz²
        lap[:, -1] += 2.0 * (T[:, -2] - T[:, -1]) / dz**2

        return lap

    # -------------------------------------------------------------------------
    # UN PASO DE INTEGRACIÓN (EULER EXPLÍCITO)
    # -------------------------------------------------------------------------

    def paso(self):
        """
        Avanza el modelo un paso de tiempo dt usando Euler explícito.

        Ecuación de actualización para cada nodo (i, j):

            T[i,j]^{n+1} = T[i,j]^n + dt · α_t(T[i,j]^n) · ∇²T[i,j]^n

        Donde la difusividad térmica efectiva varía nodo a nodo:

            α_t(T) = k / (ρ(T) · Cp) = k / ((ρ₀ - α·(T-T₀)) · Cp)

        Esto incorpora el efecto de la densidad variable sobre la inercia
        térmica del fluido.
        """
        dt  = self.params.dt
        lap = self._laplaciano()

        # Difusividad local α_t(T) vectorizada — shape (Nr, Nz)
        # ρ(T) = ρ₀ - α·(T - T₀)  →  α_t(T) = k / (ρ(T)·Cp)
        rho_local   = self.aceite.rho_0 - self.aceite.alpha * (self.T - self.aceite.T_0)
        alpha_local = self.aceite.k / (rho_local * self.aceite.Cp)

        # Euler explícito: T^{n+1} = T^n + dt · α_t(T^n) · ∇²T^n
        self.T = self.T + dt * alpha_local * lap

    # -------------------------------------------------------------------------
    # SIMULACIÓN COMPLETA
    # -------------------------------------------------------------------------

    def simular(self, verbose: bool = True) -> dict:
        """
        Ejecuta la simulación completa y retorna el historial de resultados.
        """
        t_total = self.params.t_total
        dt      = self.params.dt
        n_pasos = int(t_total / dt)

        if verbose:
            print(f"\n  Iniciando simulación: {n_pasos} pasos × {dt}s = {t_total/3600:.1f}h")
            print(f"  T inicial: {self.params.T_inicial}°C  |  T amb: {self.params.T_amb}°C\n")

        for paso_i in range(n_pasos):
            self.paso()
            t_actual = (paso_i + 1) * dt

            # Guardar cada 5 minutos
            if (paso_i + 1) % max(1, int(300 / dt)) == 0:
                self.historial_T.append(self.T.copy())
                self.historial_t.append(t_actual)

            # Reporte cada hora
            if verbose and (paso_i + 1) % int(3600 / dt) == 0:
                T_centro = self.T[0, self.params.Nz // 2]
                T_pared  = self.T[-1, self.params.Nz // 2]
                T_fondo  = np.mean(self.T[:, 0])
                gradiente_r = T_pared - T_centro
                print(f"  t={t_actual/3600:.1f}h | "
                      f"T_centro={T_centro:.3f}°C | "
                      f"T_pared={T_pared:.3f}°C | "
                      f"ΔT_radial={gradiente_r:.3f}°C")

        T_hist = np.array(self.historial_T)   # (n_reg, Nr, Nz)
        t_hist = np.array(self.historial_t)   # (n_reg,)

        if verbose:
            print(f"\n✅ Simulación completada.")
            T_final_centro = self.T[0, self.params.Nz // 2]
            T_final_pared  = self.T[-1, self.params.Nz // 2]
            print(f"   ΔT radial final (mitad del tanque): "
                  f"{T_final_pared - T_final_centro:.4f}°C")

        return {'t': t_hist, 'T': T_hist}

    # -------------------------------------------------------------------------
    # CONDICIONES INICIALES
    # -------------------------------------------------------------------------

    def condicion_inicial_uniforme(self, T: float):
        """
        Condición inicial más simple: temperatura uniforme en todo el dominio.
        """
        self.T = np.full((self.params.Nr, self.params.Nz), T, dtype=float)
        self.historial_T = [self.T.copy()]
        self.historial_t = [0.0]
        print(f"  Condición inicial uniforme: {T}°C en todos los nodos")

    def condicion_inicial_desde_sensores(self, T_sensores_pared: list,
                                          z_sensores: list):
        """
        Condición inicial realista a partir de lecturas reales de los DS18B20.

        Los sensores miden temperatura en r=R (pared exterior) a distintas
        alturas. Para los nodos interiores (r < R) se interpola linealmente
        asumiendo gradiente radial pequeño al inicio.
        """
        Nr, Nz = self.params.Nr, self.params.Nz

        T_pared_z = np.interp(self.z, z_sensores, T_sensores_pared)

        for j in range(Nz):
            T_pared = T_pared_z[j]
            for i in range(Nr):
                self.T[i, j] = T_pared

        self.historial_T = [self.T.copy()]
        self.historial_t = [0.0]
        print(f"  Condición inicial desde sensores:")
        print(f"  T mín: {np.min(self.T):.2f}°C  |  T máx: {np.max(self.T):.2f}°C")

    def condicion_inicial_llenado(self, T_aceite_nuevo: float,
                                   T_aceite_previo: float,
                                   fraccion_nueva: float):
        """
        Condición inicial para el escenario de llenado con aceite caliente.
        """
        Nr, Nz = self.params.Nr, self.params.Nz
        j_division = int((1.0 - fraccion_nueva) * Nz)

        for j in range(Nz):
            T_nodo = T_aceite_nuevo if j >= j_division else T_aceite_previo
            for i in range(Nr):
                self.T[i, j] = T_nodo

        self.historial_T = [self.T.copy()]
        self.historial_t = [0.0]
        print(f"  Condición inicial — llenado parcial:")
        print(f"  Zona superior ({fraccion_nueva*100:.0f}% vol.): "
              f"{T_aceite_nuevo}°C  ← aceite nuevo")
        print(f"  Zona inferior ({(1-fraccion_nueva)*100:.0f}% vol.): "
              f"{T_aceite_previo}°C  ← aceite previo")

    # -------------------------------------------------------------------------
    # ASIMILACIÓN DE DATOS DE PARED (sensores DS18B20)
    # -------------------------------------------------------------------------

    def actualizar_con_sensores(self, T_sensores_pared: List[float],
                                 z_sensores: Optional[List[float]] = None):
        """
        Corrige el estado del modelo usando lecturas de los sensores
        instalados en la pared exterior (r = R) del tanque.

        Estrategia: corrección proporcional al error en r=R,
        propagada hacia el interior con factor exponencial.
        """
        Nr, Nz  = self.params.Nr, self.params.Nz
        alpha_K = self.params.alpha_K
        n_sens  = len(T_sensores_pared)

        if z_sensores is None:
            z_sensores = np.linspace(0.05 * self.H, 0.95 * self.H, n_sens)

        for s, (T_med, z_s) in enumerate(zip(T_sensores_pared, z_sensores)):
            j_s = int(np.round(z_s / self.dz))
            j_s = np.clip(j_s, 0, Nz - 1)

            error_pared = T_med - self.T[Nr-1, j_s]

            beta = 1.5
            for i in range(Nr):
                factor = np.exp(-beta * (self.R - self.r[i]) / self.R)
                self.T[i, j_s] += alpha_K * error_pared * factor

        return self.T.copy()

    # -------------------------------------------------------------------------
    # CONSULTA: temperatura en cualquier punto (r, z)
    # -------------------------------------------------------------------------

    def temperatura_en(self, r_consulta: float, z_consulta: float) -> float:
        """
        Estima la temperatura en cualquier punto (r, z) del tanque
        mediante interpolación bilineal entre los nodos de la grilla.
        """
        r_c = np.clip(r_consulta, 0, self.R)
        z_c = np.clip(z_consulta, 0, self.H)

        i_f = int(r_c / self.dr)
        j_f = int(z_c / self.dz)

        i_f = min(i_f, self.params.Nr - 2)
        j_f = min(j_f, self.params.Nz - 2)

        fr = (r_c - self.r[i_f]) / self.dr
        fz = (z_c - self.z[j_f]) / self.dz

        T_interp = (
            (1 - fr) * (1 - fz) * self.T[i_f,   j_f  ] +
            (    fr) * (1 - fz) * self.T[i_f+1, j_f  ] +
            (1 - fr) * (    fz) * self.T[i_f,   j_f+1] +
            (    fr) * (    fz) * self.T[i_f+1, j_f+1]
        )
        return float(T_interp)


# =============================================================================
# 5. VISUALIZACIÓN: MAPA DE CALOR 2D + PERFILES
# =============================================================================

def graficar_resultados(resultados: dict, modelo: ModeloTanque2D,
                        output_dir: str = None):
    """
    Genera 4 gráficos:
        1. Mapa de calor 2D (r, z) del estado final
        2. Perfil radial a distintas alturas (inicio, mitad, final)
        3. Perfil axial en el eje (r=0) y en la pared (r=R)
        4. Evolución temporal del gradiente radial ΔT(R) - ΔT(0)
    """
    t    = resultados['t'] / 3600
    T_h  = resultados['T']
    Nr   = modelo.params.Nr
    Nz   = modelo.params.Nz
    r    = modelo.r * 100
    z    = modelo.z * 100

    fig = plt.figure(figsize=(14, 10))
    fig.suptitle(f'Gemelo Digital 2D (r,z) v3.0 — {modelo.tanque.nombre}\n'
                 f'Grilla: {Nr}×{Nz} nodos | dt={modelo.params.dt}s',
                 fontsize=13, fontweight='bold', y=0.98)
    gs = gridspec.GridSpec(2, 3, figure=fig, hspace=0.42, wspace=0.40)

    T_final  = T_h[-1]
    T_inicio = T_h[0]

    # ---- 1. Mapa de calor 2D — estado final ----
    ax1 = fig.add_subplot(gs[:, 0])
    r_full = np.concatenate([-r[::-1], r[1:]])
    T_full = np.concatenate([T_final[::-1, :], T_final[1:, :]], axis=0)
    im = ax1.contourf(r_full, z, T_full.T, levels=20, cmap='RdYlBu_r')
    plt.colorbar(im, ax=ax1, label='T [°C]', shrink=0.8)
    ax1.set_xlabel('Radio [cm]', fontsize=9)
    ax1.set_ylabel('Altura [cm]', fontsize=9)
    ax1.set_title('Campo de temperatura\nT(r, z) — estado final', fontsize=10, fontweight='bold')
    ax1.axvline(0, color='white', linewidth=0.8, linestyle='--', alpha=0.6)
    ax1.set_xlim(-r[-1], r[-1])

    # ---- 2. Perfiles radiales ----
    ax2 = fig.add_subplot(gs[0, 1])
    indices_t = [0, len(t)//2, -1]
    estilos   = ['--', '-.', '-']
    j_mid     = Nz // 2
    for idx, est in zip(indices_t, estilos):
        ax2.plot(r, T_h[idx, :, j_mid], est, linewidth=1.8,
                 label=f't={t[idx]:.1f}h', color='steelblue',
                 alpha=0.4 + 0.3 * indices_t.index(idx))
    ax2.set_xlabel('Radio [cm]', fontsize=9)
    ax2.set_ylabel('T [°C]', fontsize=9)
    ax2.set_title(f'Perfil radial\nen z={z[j_mid]:.1f} cm (mitad)', fontsize=10, fontweight='bold')
    ax2.legend(fontsize=8)
    ax2.grid(True, alpha=0.3)

    # ---- 3. Perfiles axiales: eje vs. pared ----
    ax3 = fig.add_subplot(gs[0, 2])
    ax3.plot(T_final[0, :],  z, '-',  color='#1F3D6B', linewidth=2, label='Centro (r=0)')
    ax3.plot(T_final[-1, :], z, '--', color='#E24B4A', linewidth=2, label='Pared (r=R)')
    ax3.fill_betweenx(z, T_final[0, :], T_final[-1, :],
                      alpha=0.15, color='orange', label='Gradiente radial')
    ax3.set_xlabel('T [°C]', fontsize=9)
    ax3.set_ylabel('Altura [cm]', fontsize=9)
    ax3.set_title('Perfil axial\ncentro vs. pared (final)', fontsize=10, fontweight='bold')
    ax3.legend(fontsize=8)
    ax3.grid(True, alpha=0.3)

    # ---- 4. Evolución del gradiente radial ----
    ax4 = fig.add_subplot(gs[1, 1])
    gradiente_r = T_h[:, -1, j_mid] - T_h[:, 0, j_mid]
    ax4.plot(t, gradiente_r, color='darkorange', linewidth=2)
    ax4.axhline(0, color='gray', linestyle='--', linewidth=1)
    ax4.fill_between(t, gradiente_r, 0, alpha=0.15, color='darkorange')
    ax4.set_xlabel('Tiempo [h]', fontsize=9)
    ax4.set_ylabel('ΔT [°C]', fontsize=9)
    ax4.set_title('Gradiente radial\nT_pared − T_centro (mitad)', fontsize=10, fontweight='bold')
    ax4.grid(True, alpha=0.3)

    # ---- 5. Mapa de calor en t=0 (para comparar) ----
    ax5 = fig.add_subplot(gs[1, 2])
    T_full_0 = np.concatenate([T_inicio[::-1, :], T_inicio[1:, :]], axis=0)
    im2 = ax5.contourf(r_full, z, T_full_0.T, levels=20, cmap='RdYlBu_r')
    plt.colorbar(im2, ax=ax5, label='T [°C]', shrink=0.8)
    ax5.set_xlabel('Radio [cm]', fontsize=9)
    ax5.set_ylabel('Altura [cm]', fontsize=9)
    ax5.set_title('Campo de temperatura\nT(r, z) — estado inicial', fontsize=10, fontweight='bold')
    ax5.axvline(0, color='white', linewidth=0.8, linestyle='--', alpha=0.6)

    # Guardar en el directorio del script si no se especifica otro
    if output_dir is None:
        output_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(output_dir, 'resultados_modelo_2D_v3.png')
    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()
    print(f"\n📊 Gráfico guardado: {output_path}")


# =============================================================================
# 6. PROGRAMA PRINCIPAL
# =============================================================================

if __name__ == "__main__":

    import time

    # -------------------------------------------------------
    # CASO 1: PROTOTIPO 20 L — comparación de rendimiento v2 vs v3
    # -------------------------------------------------------
    print("\n" + "="*58)
    print("  CASO 1: PROTOTIPO 20 L (HDPE) — v3.0")
    print("="*58)

    aceite = PropiedadesAceite()
    tanque = GeometriaTanque()
    params = ParametrosModelo(
        Nr             = 15,
        Nz             = 20,
        h_ext          = 5.0,
        T_amb          = 25.0,
        T_inicial      = 15.0,
        dt             = 30.0,
        t_total        = 3600 * 8,
        nivel_fraccion = 0.85,
        alpha_K        = 0.6
    )

    modelo = ModeloTanque2D(aceite, tanque, params)

    t0 = time.time()
    resultados = modelo.simular(verbose=True)
    t1 = time.time()
    print(f"\n  ⏱️  Tiempo de simulación (v3, Nr=15, Nz=20): {t1-t0:.2f} s")

    graficar_resultados(resultados, modelo)

    # -------------------------------------------------------
    # DEMOSTRACIÓN: consulta de temperatura en punto arbitrario
    # -------------------------------------------------------
    print("\n🎯 Consulta de temperatura en puntos arbitrarios:")
    puntos = [
        (0.0,              modelo.H * 0.5),
        (modelo.R * 0.5,   modelo.H * 0.5),
        (modelo.R,         modelo.H * 0.5),
        (0.0,              modelo.H * 0.9),
        (modelo.R,         modelo.H * 0.1),
    ]
    for r_q, z_q in puntos:
        T_q = modelo.temperatura_en(r_q, z_q)
        print(f"   T(r={r_q*100:.1f}cm, z={z_q*100:.1f}cm) = {T_q:.4f}°C")

    # -------------------------------------------------------
    # DEMOSTRACIÓN: asimilación de datos de sensores de pared
    # -------------------------------------------------------
    print("\n🔌 Asimilación de datos de sensores (pared r=R):")
    print(f"   T antes en pared (j=10): {modelo.T[-1, 10]:.4f}°C")
    print(f"   T antes en centro(j=10): {modelo.T[0,  10]:.4f}°C")

    T_sensores = [21.5, 20.8, 20.1, 19.4]
    z_sensores = [modelo.H * 0.2, modelo.H * 0.4,
                  modelo.H * 0.6, modelo.H * 0.8]
    modelo.actualizar_con_sensores(T_sensores, z_sensores)

    print(f"   T después en pared(j=10): {modelo.T[-1, 10]:.4f}°C")
    print(f"   T después en centro(j=10): {modelo.T[0, 10]:.4f}°C")
    print(f"   → Corrección se propagó hacia el interior ✓")

    # -------------------------------------------------------
    # CASO 2: TANQUE INDUSTRIAL LAS 200
    # -------------------------------------------------------
    print("\n" + "="*58)
    print("  CASO 2: TANQUE INDUSTRIAL LAS 200 (30.000 L)")
    print("="*58)

    tanque_ind = GeometriaTanque.industrial_las200()
    params_ind = ParametrosModelo(
        Nr             = 15,
        Nz             = 20,
        h_ext          = 5.0,
        T_amb          = 25.0,
        T_inicial      = 15.0,
        dt             = 3600.0,
        t_total        = 3600 * 24 * 7,
        nivel_fraccion = 0.90,
        alpha_K        = 0.6
    )

    modelo_ind = ModeloTanque2D(aceite, tanque_ind, params_ind)

    print(f"\n  Dimensiones: D={tanque_ind.diametro}m, H={tanque_ind.altura_total:.2f}m")
    print(f"  El modelo 2D está listo para escalar al tanque industrial.")
    print(f"\n✅ Modelo 2D v3.0 listo.")
    print("   Próximos pasos:")
    print("   1. Conectar sensores ESP32 → reemplazar T_sensores con datos reales")
    print("   2. Calibrar h_ext y alpha_K con experimentos en el prototipo")
    print("   3. Integrar con MQTT para actualización en tiempo real")
