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
nivel y masa del aceite, a partir de las temperaturas medidas en la pared exterior
del tanque. Los resultados se visualizan en un dashboard Grafana y un heatmap 2D
generado por el modelo.

---

## Arquitectura del sistema

**Capa 1 — Raspberry Pi `sensor` (192.168.1.106)**
- 5× DS18B20 en pared exterior (DS0–DS4), 2× DS18B20 ambiente (DS_AMB1, DS_AMB2), 1× DS18B20 tanque superior (DS_SUP)
- HC-SR04 (nivel), HX711 + celda de carga (masa), Display OLED
- Publica JSON por MQTT cada 10 segundos al broker en `gemelo5`
- Corre como `sensor.service` (systemd)

**Capa 2 — Raspberry Pi `gemelo5` (192.168.1.104)**
- Broker Mosquitto recibe los mensajes MQTT
- Suscriptor Python valida rangos físicos y escribe en InfluxDB bucket `gemelo`
- Corre como `suscriptor.service` (systemd)

**Capa 3 — Raspberry Pi `gemelo5`**
- Modelo 2D axisimétrico T(r,z,t) en tiempo real
- Lee temperaturas de pared desde InfluxDB, estima la distribución interior del fluido
- Sirve heatmap PNG en puerto 5000 via Flask
- Corre como `modelo.service` (systemd)

**Capa 4 — Raspberry Pi `gemelo5`**
- Dashboard Grafana conectado a InfluxDB
- Kiosco en monitor conectado a la Pi 5

> La Raspberry Pi `gemelo5` es el servidor provisional.
> La migración al servidor del laboratorio UACh (con Docker) está planificada.

---

## Hardware (Prototipo)

| Componente | Función | Capa |
|---|---|---|
| Raspberry Pi Zero 2 W (`sensor`) | Adquisición de datos | 1 |
| Raspberry Pi 5 (`gemelo5`) | Servidor provisional | 2, 3, 4 |
| 5× DS18B20 (DS0–DS4) | Temperatura pared exterior (0–30 cm) | 1 |
| 2× DS18B20 (DS_AMB1, DS_AMB2) | Temperatura ambiente (promediadas) | 1 |
| 1× DS18B20 (DS_SUP) | Temperatura fluido tanque superior | 1 |
| HC-SR04 | Nivel ultrasónico | 1 |
| HX711 + celda de carga | Masa | 1 |
| Display OLED 128×64 I2C | Visualización local | 1 |

---

## Estructura del repositorio

```
gemelo-digital-aceite-oliva/
├── config.py                          # Configuración central (único lugar para cambiar parámetros)
├── .env                               # Token InfluxDB — NO se commitea (ver .env.example)
├── .env.example                       # Plantilla del .env
│
├── capa1_sensor/
│   └── sensor.py                      # Script en producción — RPi Zero 2W
│
├── capa2_adquisicion/
│   ├── suscriptor.py                  # Script en producción — RPi 5
│   └── telegraf.conf                  # Pipeline alternativo (Telegraf)
│
├── capa3_modelo/
│   ├── modelo.py                      # Script en producción — RPi 5
│   ├── tanque_modelo_2D_v2.py         # Referencia — modelo standalone v2.1
│   └── tanque_modelo_2D_v3.py         # Referencia — modelo standalone v3.0
│
└── docker/
    ├── docker-compose.yml             # Orquestación (preparado, pendiente de despliegue)
    ├── modelo/
    │   ├── Dockerfile                 # Imagen del modelo (usa capa3_modelo/modelo.py)
    │   ├── requirements.txt
    │   └── modelo.py                  # OBSOLETO — reemplazado por capa3_modelo/modelo.py
    └── mosquitto/
        └── config/mosquitto.conf
```

---

## Configuración centralizada

Todos los parámetros del sistema están en **`config.py`** en la raíz del repositorio.
No es necesario modificar los scripts para cambiar IPs, topics, IDs de sensores o parámetros del modelo.

```python
# Ejemplos de parámetros en config.py
MQTT_BROKER_SENSOR = "192.168.1.104"   # IP del gemelo (usado por sensor)
MODELO_IC_DEFAULT  = "t_sup"           # Condición inicial al arrancar: "t_sup" | "sensores"
MODELO_ALPHA_K     = 0.6               # Ganancia asimilación de datos
```

### Secretos (.env)

El token de InfluxDB **no va en el código**. Se define en un archivo `.env`:

```bash
# .env (no commitear)
INFLUX_TOKEN=your_influxdb_token_here
```

Crear a partir de la plantilla:
```bash
cp .env.example .env
# editar .env con el token real
```

En producción, systemd lo carga via `EnvironmentFile=/home/sebar/.env` en el override de cada servicio.
En Docker, lo inyecta `env_file: ../.env` en el `docker-compose.yml`.

---

## Modelo termofísico (Capa 3)

El modelo resuelve la **ecuación de calor en coordenadas cilíndricas** para un fluido estático:

```
ρ(T)·Cp·∂T/∂t = k·[1/r·∂/∂r(r·∂T/∂r) + ∂²T/∂z²]
```

A partir de las temperaturas medidas en la pared (DS0–DS4), estima T(r,z) en el interior del fluido.
En cada ciclo los nodos de pared se corrigen hacia las mediciones reales (asimilación de datos).

### Condiciones de contorno

| Borde | Condición |
|-------|-----------|
| r = 0 | Simetría axial (L'Hôpital) |
| r = R | Robin + corrección por asimilación de datos (DS0–DS4) |
| z = 0 | Fondo adiabático |
| z = H | Superficie libre adiabática |

### Parámetros del modelo

| Parámetro | Valor | Descripción |
|-----------|-------|-------------|
| Nr × Nz | 15 × 20 | Nodos radiales × axiales |
| dt | ≤ 30 s | Paso temporal (Von Neumann estable) |
| h_ext | 5.0 W/(m²·°C) | Convección exterior — pendiente calibración |
| alpha_K | 0.6 | Ganancia asimilación de datos |
| IC default | `t_sup` | Condición inicial al arrancar el servicio |

### Propiedades del aceite (Ribeiro et al. 2017, Fasina y Colley 2008)

- ρ(T) = 912.66 − 0.0803·T [kg/m³] — densidad variable
- Cp = 1970 J/(kg·°C)
- k = 0.17 W/(m·°C)
- Válido para T ∈ [10°C, 40°C]

### Versiones de referencia (archivos standalone)

| Versión | Grilla | Laplaciano | Archivo |
|---------|--------|------------|---------|
| v2.1 | Nr=3, Nz=7 | Loops Python | `tanque_modelo_2D_v2.py` |
| v3.0 | Nr=15, Nz=20 | Vectorizado NumPy (~50× más rápido) | `tanque_modelo_2D_v3.py` |

### Limitación conocida

Ra ≈ 1.4×10⁶ indica que la convección natural es significativa. El modelo asume fluido estático (conducción pura). Incorporar convección natural queda propuesto como trabajo futuro.

---

## Configuración de red

| Dispositivo | IP | Red |
|---|---|---|
| Raspberry `sensor` | 192.168.1.106 | wifi_control.iee |
| Raspberry `gemelo5` | 192.168.1.104 | wifi_control.iee |

| Servicio | URL |
|----------|-----|
| Grafana dashboard | http://192.168.1.104:3000 |
| Heatmap modelo | http://192.168.1.104:5000/heatmap |
| InfluxDB | http://192.168.1.104:8086 |

---

## Pines físicos (Raspberry Pi Zero 2 W)

| Sensor | Pin físico | GPIO BCM |
|---|---|---|
| DS18B20 DATA (todos) | 7 | GPIO 4 |
| HC-SR04 TRIG | 18 | GPIO 24 |
| HC-SR04 ECHO | 22 | GPIO 25 |
| HX711 DT | 21 | GPIO 9 |
| HX711 SCK | 23 | GPIO 11 |
| OLED SDA | 3 | GPIO 2 |
| OLED SCL | 5 | GPIO 3 |
| Relé bomba (IN) | 40 | GPIO 21 |

---

## Despliegue en producción (systemd)

Los scripts corren como servicios systemd en sus respectivas RPis.

### Rutas en producción

| Script | Ruta en RPi |
|--------|------------|
| `sensor.py` | `/home/sebar/sensor/sensor.py` (RPi Zero) |
| `config.py` | `/home/sebar/sensor/config.py` (RPi Zero) |
| `suscriptor.py` | `/home/sebar/gemelo/suscriptor.py` (RPi 5) |
| `modelo.py` | `/home/sebar/modelo/modelo.py` (RPi 5) |
| `config.py` | `/home/sebar/config.py` (RPi 5 — padre común) |
| `.env` | `/home/sebar/.env` (RPi 5 — no en git) |

### Actualizar scripts en producción

Desde Git Bash en Windows, después de hacer `git pull`:

```bash
# RPi 5
scp config.py sebar@192.168.1.104:/home/sebar/config.py
scp capa3_modelo/modelo.py sebar@192.168.1.104:/home/sebar/modelo/modelo.py
scp capa2_adquisicion/suscriptor.py sebar@192.168.1.104:/home/sebar/gemelo/suscriptor.py

# RPi Zero
scp config.py sebar@192.168.1.106:/home/sebar/sensor/config.py
scp capa1_sensor/sensor.py sebar@192.168.1.106:/home/sebar/sensor/sensor.py
```

Reiniciar servicios en RPi 5:
```bash
sudo systemctl restart modelo.service suscriptor.service
```

Reiniciar en RPi Zero:
```bash
sudo systemctl restart sensor.service
```

---

## Pipeline de datos

**Opción A — Suscriptor Python** (activa, con validación de rangos físicos)
- Escribe en bucket: `gemelo`

**Opción B — Telegraf** (alternativa, sin validación)
- Configuración: `capa2_adquisicion/telegraf.conf`
- Escribe en bucket: `gemelo_telegraf`

---

## Comandos MQTT

### Control del sensor (topic: `tanque/cmd`)

```bash
# Rehacer tara de la celda de carga (tanque vacío)
mosquitto_pub -h 192.168.1.104 -t tanque/cmd -m "tara"

# Encender bomba manualmente (se mantiene hasta bomba/off)
mosquitto_pub -h 192.168.1.104 -t tanque/cmd -m "bomba/on"

# Apagar bomba
mosquitto_pub -h 192.168.1.104 -t tanque/cmd -m "bomba/off"

# Llenado automático: enciende 13 min → apaga → notifica al modelo (inicio/sup)
mosquitto_pub -h 192.168.1.104 -t tanque/cmd -m "bomba/llenar"
```

### Control del modelo (topic: `modelo/cmd`)

```bash
# Reiniciar con temperatura del tanque superior (condición inicial uniforme)
mosquitto_pub -h 192.168.1.104 -t modelo/cmd -m "inicio/sup"

# Reiniciar con interpolación desde sensores de pared
mosquitto_pub -h 192.168.1.104 -t modelo/cmd -m "reset"

# Cambiar fluido simulado
mosquitto_pub -h 192.168.1.104 -t modelo/cmd -m "fluido/agua"
mosquitto_pub -h 192.168.1.104 -t modelo/cmd -m "fluido/aceite"
```

### Flujo de trabajo para experimento de llenado

**Automático (recomendado):**
1. Enviar `bomba/llenar` → la bomba enciende 13 min, apaga sola y notifica al modelo (`inicio/sup`)
2. Abrir válvula — el fluido entra desde el tanque superior ya a temperatura conocida
3. El modelo evoluciona desde T_sup hacia el gradiente real, corrigiéndose con los sensores de pared

**Manual:**
1. Enviar `bomba/on` → llenar el tanque superior
2. Cuando esté lleno, enviar `bomba/off`
3. Enviar `inicio/sup` al modelo para establecer la condición inicial
4. Abrir válvula

---

## Despliegue con Docker (pendiente)

Para migrar al servidor del laboratorio.

### Requisitos

- Docker y Docker Compose instalados
- Crear `.env` con `INFLUX_TOKEN` antes de arrancar (ver `.env.example`)
- Puertos disponibles: 1883, 8086, 3000, 5000

### Arrancar el stack

```bash
cd docker
docker-compose up -d
```

### Detener

```bash
docker-compose down
```

### Variables de entorno del modelo

| Variable | Valor en Docker | Valor local |
|---|---|---|
| `INFLUX_URL` | `http://influxdb:8086` | `http://localhost:8086` |
| `MQTT_BROKER` | `mosquitto` | `localhost` |
| `INFLUX_TOKEN` | desde `.env` | desde `.env` |

### Notas

- El Dockerfile usa `capa3_modelo/modelo.py` directamente — no hay modelo duplicado
- InfluxDB se inicializa con org=`uach`, bucket=`gemelo`
- Grafana arranca con acceso anónimo habilitado
- La RPi `sensor` apunta a la IP del servidor donde corre Docker (cambiar `MQTT_BROKER_SENSOR` en `config.py`)

---

## Referencias

- Ribeiro et al. (2017). Eur. J. Lipid Sci. Technol., 119(5)
- Fasina y Colley (2008). Int. J. Food Properties, 11(4)
- Turgut et al. (2009). Int. J. Food Properties, 12(4)
- Çengel, Y.A. (2007). Transferencia de calor y masa, 3ª ed.
