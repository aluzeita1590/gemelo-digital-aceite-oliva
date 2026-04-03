# Gemelo Digital — Tanque de Almacenamiento de Aceite de Oliva

Trabajo de Titulación · Ingeniería Civil Electrónica
**Autor:** Sebastian Araneda · **Guía:** Dr. José Saavedra
**Empresa colaboradora:** Las 200 · Chile, 2025

---

## Descripción

Este repositorio contiene el desarrollo de un **gemelo digital** para un tanque prototipo de 20 litros de aceite de oliva. El sistema integra sensores IoT con un modelo matemático termofísico para estimar en tiempo real la distribución de temperatura, nivel y densidad del aceite al interior del tanque, sin necesidad de instrumentación interna.

El prototipo sirve como banco de pruebas para validar los modelos físicos y el sistema de adquisición de datos, con miras a una futura escalabilidad a los tanques industriales de Las 200 (~30.000 L).

---

## Arquitectura del sistema

```
┌─────────────────────────────────────────────────────────┐
│  Capa 1 — Adquisición (ESP32)                           │
│  DS18B20 × 5 · HC-SR04 · Celda de carga HX711          │
│  Protocolo: MQTT sobre Wi-Fi                            │
├─────────────────────────────────────────────────────────┤
│  Capa 2 — Comunicación (MQTT Broker)                    │
│  tópicos: tanque/temperatura · tanque/nivel · tanque/peso│
├─────────────────────────────────────────────────────────┤
│  Capa 3 — Modelo termofísico (Python)                   │
│  Ecuación de calor 2D axisimétrica · Diferencias finitas│
│  Asimilación de datos con corrección por sensores       │
└─────────────────────────────────────────────────────────┘
```

---

## Hardware (prototipo)

| Sensor | Variable | Interfaz |
|--------|----------|----------|
| DS18B20 × 5 | Temperatura en pared exterior (5 niveles) | 1-Wire (GPIO4) |
| HC-SR04 | Nivel de aceite (distancia) | GPIO Trig/Echo |
| Celda de carga + HX711 | Masa total del aceite | SPI/I²C |
| ESP32 DevKit | Adquisición y comunicación | Wi-Fi / MQTT |

Los sensores DS18B20 se montan en la **pared exterior** del tanque HDPE, cubiertos con espuma aislante para evitar influencia del aire ambiente. La distribución vertical es uniforme a lo largo de la columna de aceite.

---

## Modelo termofísico (Capa 3)

Resuelve la **ecuación de calor en coordenadas cilíndricas** para un fluido estático:

```
ρ(T)·Cp·∂T/∂t = k·[1/r·∂/∂r(r·∂T/∂r) + ∂²T/∂z²]
```

Integración temporal con **Euler explícito**. Densidad variable `ρ(T)` según Ribeiro et al. (2017).

### Condiciones de contorno

| Borde | Condición |
|-------|-----------|
| r = 0 | Simetría axial (L'Hôpital) |
| r = R | Robin — convección exterior con los DS18B20 |
| z = 0 | Fondo adiabático |
| z = H | Superficie libre adiabática |

### Versiones del modelo

| Versión | Grilla | Laplaciano | Notas |
|---------|--------|------------|-------|
| v2.1 | Nr=3, Nz=7 | Loops Python | Versión inicial |
| **v3.0** | **Nr=15, Nz=20** | **Vectorizado NumPy** | Versión activa — ~50× más rápido |

### Limitación conocida

El número de Rayleigh del prototipo (Ra ≈ 1.4×10⁶) indica que la convección natural es significativa. El modelo actual asume fluido estático (conducción pura), lo que constituye una limitación documentada. La incorporación de convección natural queda propuesta como trabajo futuro.

---

## Estructura del repositorio

```
gemelo-digital-aceite-oliva/
├── capa1_sensor/
│   └── tanque_esp32.ino          # Firmware ESP32: lectura de sensores y MQTT
├── capa3_modelo/
│   ├── tanque_modelo_2D_v2.py    # Modelo inicial (referencia)
│   ├── tanque_modelo_2D_v3.py    # Modelo activo (grilla fina + vectorizado)
│   └── resultados_modelo_2D_v3.png  # Gráficos de la última simulación
└── README.md
```

> **Nota:** La capa 2 (broker MQTT) se ejecuta con Mosquitto en el computador local. No requiere código adicional en este repositorio.

---

## Cómo ejecutar el modelo

```bash
# Instalar dependencias
pip install numpy matplotlib

# Ejecutar simulación de 8 horas (prototipo 20 L)
python capa3_modelo/tanque_modelo_2D_v3.py
```

El script genera automáticamente `resultados_modelo_2D_v3.png` con el mapa de temperatura 2D, perfiles radiales y axiales, y la evolución temporal del gradiente radial.

---

## Estado del proyecto

- [x] Propuesta y marco teórico
- [x] Especificación de instrumentación (ESP32 + sensores)
- [x] Firmware ESP32 (capa 1)
- [x] Modelo termofísico 2D axisimétrico v3.0 (capa 3)
- [ ] Ensamble del prototipo físico
- [ ] Calibración experimental de `h_ext` y `alpha_K`
- [ ] Dashboard de visualización en tiempo real (capa 4)
- [ ] Validación experimental (objetivo: error < 1°C)
- [ ] Escalabilidad a tanque industrial Las 200

---

## Referencias principales

- Ribeiro et al. (2017). *Eur. J. Lipid Sci. Technol.*, 119(5).
- Fasina et al. (2008). Propiedades termofísicas aceites vegetales.
- Çengel, Y.A. (2007). *Transferencia de calor y masa*, 3ª ed.
- Incropera et al. (2007). *Fundamentals of Heat and Mass Transfer*, 6th ed.
