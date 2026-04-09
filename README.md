# Gemelo Digital — Tanque de Almacenamiento de Aceite de Oliva

**Trabajo de Titulación — Ingeniería Civil Electrónica, UACh**  
Empresa colaboradora: Las 200  
Autor: Sebastián Araneda

---

## Descripción

Sistema de gemelo digital para un tanque prototipo de 20 L de aceite de oliva, 
organizado en una arquitectura de 4 capas escalable a los tanques industriales 
de 30.000 L de Las 200.

El sistema estima en tiempo real la distribución espacial de temperatura T(r,z,t), 
nivel y masa del aceite, visualizando los resultados en un dashboard Grafana.

---

## Arquitectura del sistema

```
┌──────────────────────────────────────────────────────────────┐
│  Raspberry Pi sensor (Capa 1)                                |
|└── DS18B20 ×5, HC-SR04, HX711, Display OLED                  |
|└── Publica por MQTT                                          │
├──────────────────────────────────────────────────────────────┤
│  Raspberry Pi gemelo (Capas 2, 3 y 4) — servidor provisional |
|└── Mosquitto (broker MQTT)                                   |
|└── Suscriptor Python → InfluxDB                              |
|└── Modelo 2D axisimétrico (r,z)                              |
|└── Grafana → dashboard en tiempo real│                       |
├──────────────────────────────────────────────────────────────┤
│  Servidor UACh (futuro) — reemplazará a la Raspberry gemelo  │
└──────────────────────────────────────────────────────────────┘
```

## Hardware (Prototipo)

| Componente | Función | Capa |
|---|---|---|
| Raspberry Pi Zero 2 W (`sensor`) | Adquisición de datos | 1 |
| Raspberry Pi Zero 2 W (`gemelo`) | Servidor provisional | 2, 3, 4 |
| 5× DS18B20 | Temperatura pared exterior | 1 |
| HC-SR04 | Nivel ultrasónico | 1 |
| HX711 + celda de carga | Masa | 1 |
| Display OLED 128×64 I2C | Visualización local | 1 |

---

## Estructura del repositorio

gemelo-digital-aceite-oliva/
├── capa1_sensor/
│   ├── sensor.py          # Script principal Raspberry sensor
│   └── tanque_esp32.ino   # Firmware ESP32 (referencia futura)
├── capa2_adquisicion/
│   └── suscriptor.py      # Broker MQTT + validación + InfluxDB
├── capa3_modelo/
│   ├── modelo.py          # Modelo 2D en tiempo real (Capa 3)
│   ├── tanque_modelo_2D_v2.py  # Modelo standalone v2.1
│   └── tanque_modelo_2D_v3.py  # Modelo standalone v3.0
└── docs/
├── propuesta_tesis_v5.docx
└── plan_trabajo_v3.docx

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

**Propiedades del aceite (Ribeiro et al. 2017, Fasina y Colley 2008):**
- ρ(T) = 912.66 − 0.0803·T [kg/m³]
- Cp = 1970 J/(kg·°C)
- k = 0.17 W/(m·°C)
- Válido para T ∈ [10°C, 40°C]

**Discretización:** diferencias finitas centradas + Euler explícito  
**Grilla:** 15×20 nodos (Nr×Nz)  
**Condiciones de contorno:** simetría axial en r=0 (L'Hôpital), Robin en r=R, adiabático en z=0 y z=H

**Limitación documentada:** conducción pura sin convección natural (Ra ≈ 5×10⁶)

---

## Configuración de red (laboratorio UACh)

| Dispositivo | IP | Red |
|---|---|---|
| Raspberry `sensor` | 192.168.1.106 | TESTMEDIOS |
| Raspberry `gemelo` | 192.168.1.105 | TESTMEDIOS |

Acceso a Grafana: `http://192.168.1.105:3000`  
Acceso al heatmap del modelo: `http://192.168.1.105:5000/heatmap`

> **Nota:** El servidor provisional es una Raspberry Pi Zero 2 W. 
> La migración al servidor del laboratorio UACh está planificada como paso siguiente.

---

## Pines físicos (Raspberry Pi Zero 2 W — sensor)

| Sensor | Pin físico | GPIO BCM |
|---|---|---|
| DS18B20 DATA | 7 | GPIO 4 |
| HC-SR04 TRIG | 18 | GPIO 24 |
| HC-SR04 ECHO | 22 | GPIO 25 |
| HX711 DT | 21 | GPIO 9 |
| HX711 SCK | 23 | GPIO 11 |
| OLED SDA | 3 | GPIO 2 |
| OLED SCL | 5 | GPIO 3 |

---

## Servicios systemd

Todos los servicios arrancan automáticamente al encender:

```bash
# En gemelo
sudo systemctl status mosquitto suscriptor modelo grafana-server

# En sensor
sudo systemctl status sensor
```

## Comando tara (remoto)

```bash
mosquitto_pub -h 192.168.1.105 -t tanque/cmd -m "tara"
```

---

## Referencias

- Ribeiro et al. (2017). Eur. J. Lipid Sci. Technol., 119(5)
- Fasina y Colley (2008). Int. J. Food Properties, 11(4)
- Turgut et al. (2009). Int. J. Food Properties, 12(4)
- Çengel, Y.A. (2007). Transferencia de calor y masa, 3ª ed.
