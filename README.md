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

**Capa 1 — Raspberry Pi `sensor`**
- Sensores: DS18B20 ×5, HC-SR04, HX711, Display OLED
- Publica datos por MQTT cada 10 segundos

**Capa 2 — Raspberry Pi `gemelo` (servidor provisional)**
- Broker Mosquitto recibe los mensajes MQTT
- Suscriptor Python valida y almacena en InfluxDB

**Capa 3 — Raspberry Pi `gemelo`**
- Modelo 2D axisimétrico T(r,z,t) en tiempo real
- Lee sensores desde InfluxDB, estima temperatura interior

**Capa 4 — Raspberry Pi `gemelo`**
- Dashboard Grafana conectado a InfluxDB
- Heatmap del modelo disponible en puerto 5000

> La Raspberry Pi `gemelo` es el servidor provisional.
> La migración al servidor del laboratorio UACh está planificada.

---

## Hardware (Prototipo)

| Componente | Función | Capa |
|---|---|---|
| Raspberry Pi Zero 2 W (`sensor`) | Adquisición de datos | 1 |
| Raspberry Pi 5 (`gemelo`) | Servidor provisional | 2, 3, 4 |
| 5× DS18B20 | Temperatura pared exterior | 1 |
| HC-SR04 | Nivel ultrasónico | 1 |
| HX711 + celda de carga | Masa | 1 |
| Display OLED 128×64 I2C | Visualización local | 1 |

---

## Estructura del repositorio


- `capa1_sensor/sensor.py` — Script principal Raspberry sensor
- `capa1_sensor/tanque_esp32.ino` — Firmware ESP32 (referencia futura)
- `capa2_adquisicion/suscriptor.py` — Broker MQTT + validación + InfluxDB
- `capa3_modelo/modelo.py` — Modelo 2D en tiempo real
- `capa3_modelo/tanque_modelo_2D_v2.py` — Modelo standalone v2.1
- `capa3_modelo/tanque_modelo_2D_v3.py` — Modelo standalone v3.0

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

## Configuración de red (laboratorio IIoT)

| Dispositivo | IP | Red |
|---|---|---|
| Raspberry `sensor` | 192.168.1.106 | wifi_control.iee |
| Raspberry `gemelo5` | 192.168.1.104 | wifi_control.iee |

Acceso a Grafana: `http://192.168.1.104:3000`  
Acceso al heatmap del modelo: `http://192.168.1.104:5000/heatmap`


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

## Pipeline de datos

**Opción A — Suscriptor Python** (con validación de rangos físicos)
- Broker: Mosquitto en gemelo5
- Escribe en bucket: `gemelo`

**Opción B — Telegraf** (más robusto, sin validación)
- Configuración: `capa2_adquisicion/telegraf.conf`
- Escribe en bucket: `gemelo_telegraf`

## Comandos MQTT disponibles

```bash
# Cambiar fluido del modelo
mosquitto_pub -h 192.168.1.104 -t modelo/cmd -m "fluido/agua"
mosquitto_pub -h 192.168.1.104 -t modelo/cmd -m "fluido/aceite"

Todos los servicios arrancan automáticamente al encender:

# Reiniciar condición inicial del modelo
mosquitto_pub -h 192.168.1.104 -t modelo/cmd -m "reset"

# Rehacer tara de la celda de carga
mosquitto_pub -h 192.168.1.104 -t tanque/cmd -m "tara"
```


---

## Despliegue con Docker

Para migrar el sistema al servidor del laboratorio o a cualquier máquina Linux con Docker instalado.

### Requisitos
- Docker y Docker Compose instalados
- Puerto 1883 (MQTT), 8086 (InfluxDB), 3000 (Grafana) y 5000 (Modelo) disponibles

### Estructura

- `docker/docker-compose.yml` — orquestación de los 4 servicios
- `docker/modelo/` — imagen personalizada del modelo 2D Python
- `docker/mosquitto/config/` — configuración del broker MQTT

### Arrancar el stack

```bash
cd docker
docker-compose up -d
```

### Detener el stack

```bash
docker-compose down
```

### Variables de entorno del modelo

El modelo lee la configuración desde variables de entorno definidas en `docker-compose.yml`:

| Variable | Valor en Docker | Valor local |
|---|---|---|
| `INFLUX_URL` | `http://influxdb:8086` | `http://localhost:8086` |
| `MQTT_BROKER` | `mosquitto` | `localhost` |

### Notas
- InfluxDB se inicializa automáticamente con org=`uach`, bucket=`gemelo`
- El token de InfluxDB se genera al arrancar — obtenerlo con `docker exec influxdb influx auth list`
- Grafana arranca con acceso anónimo habilitado
- La Raspberry `sensor` debe apuntar a la IP del servidor donde corre Docker

## Referencias

- Ribeiro et al. (2017). Eur. J. Lipid Sci. Technol., 119(5)
- Fasina y Colley (2008). Int. J. Food Properties, 11(4)
- Turgut et al. (2009). Int. J. Food Properties, 12(4)
- Çengel, Y.A. (2007). Transferencia de calor y masa, 3ª ed.
