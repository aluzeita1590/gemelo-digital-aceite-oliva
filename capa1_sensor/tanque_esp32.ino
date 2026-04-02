/*
 * =============================================================================
 * GEMELO DIGITAL — TANQUE DE ACEITE DE OLIVA
 * Firmware ESP32 — Capa 1: Instrumentación y captura de datos
 * =============================================================================
 * Autor   : [Tu nombre]
 * Versión : 1.0
 * Fecha   : 2025
 *
 * Descripción:
 *   Lee los tres tipos de sensores del tanque prototipo y publica los datos
 *   en formato JSON al broker MQTT del servidor del laboratorio.
 *
 * Sensores:
 *   - 6× DS18B20  → temperatura en pared exterior (bus 1-Wire, GPIO 4)
 *   - HC-SR04     → nivel de aceite (GPIO 5 TRIG, GPIO 18 ECHO)
 *   - HX711       → masa total vía celda de carga (GPIO 21 DT, GPIO 22 SCK)
 *
 * Dependencias (instalar en Arduino IDE → Gestor de bibliotecas):
 *   - OneWire          (Paul Stoffregen)
 *   - DallasTemperature (Miles Burton)
 *   - HX711            (Bogdan Necula / queuetue)
 *   - PubSubClient     (Nick O'Leary)
 *   - ArduinoJson      (Benoit Blanchon) — versión 6.x
 *
 * Formato JSON publicado (topic: tanque/datos):
 *   {
 *     "ts": 1700000000,
 *     "temp": [22.06, 21.44, 20.81, 20.19, 19.56, 18.94],
 *     "nivel_m": 0.245,
 *     "masa_kg": 15.48,
 *     "n_sensores": 6,
 *     "ciclo": 123
 *   }
 * =============================================================================
 */

// ── Bibliotecas ──────────────────────────────────────────────────────────────
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include "HX711.h"

// =============================================================================
// CONFIGURACIÓN — AJUSTAR ANTES DE COMPILAR
// =============================================================================

// ── Red Wi-Fi ────────────────────────────────────────────────────────────────
const char* WIFI_SSID     = "NOMBRE_RED_WIFI";       // <-- reemplazar
const char* WIFI_PASSWORD = "CLAVE_WIFI";             // <-- reemplazar

// ── Broker MQTT (servidor del laboratorio) ───────────────────────────────────
const char* MQTT_BROKER   = "192.168.X.X";            // <-- reemplazar con IP del servidor
const int   MQTT_PORT     = 1883;
const char* MQTT_CLIENT   = "esp32_tanque_01";
// Si el broker tiene usuario/contraseña, descomenta estas líneas:
// const char* MQTT_USER  = "usuario";
// const char* MQTT_PASS  = "clave";

// ── Topics MQTT ──────────────────────────────────────────────────────────────
const char* TOPIC_DATOS   = "tanque/datos";           // datos principales
const char* TOPIC_ESTADO  = "tanque/estado";          // heartbeat / estado del ESP32
const char* TOPIC_CMD     = "tanque/cmd";             // comandos entrantes (futuro)

// ── Pines ────────────────────────────────────────────────────────────────────
#define PIN_ONE_WIRE    4     // Bus 1-Wire para DS18B20
#define PIN_TRIG        5     // HC-SR04 trigger
#define PIN_ECHO        18    // HC-SR04 echo  (¡necesita divisor resistivo 1kΩ/2kΩ!)
#define PIN_HX711_DT    21    // HX711 data
#define PIN_HX711_SCK   22    // HX711 clock

// ── Geometría del tanque prototipo ───────────────────────────────────────────
const float ALTURA_TAPA_CM  = 32.5;   // distancia tapa → fondo vacío [cm]
                                       // medir con el tanque vacío y ajustar

// ── Celda de carga ───────────────────────────────────────────────────────────
// Factor de calibración: ajustar con pesa de referencia conocida
// Procedimiento: ver función calibrarCeldaCarga() al final del archivo
float FACTOR_CALIBRACION    = 420.0;  // <-- ajustar con calibración real
const float TARA_KG         = 0.0;    // se establece automáticamente en setup()

// ── Frecuencia de muestreo ───────────────────────────────────────────────────
const unsigned long INTERVALO_MS = 10000;  // 10 segundos (ajustable)

// =============================================================================
// OBJETOS GLOBALES
// =============================================================================
OneWire           oneWire(PIN_ONE_WIRE);
DallasTemperature ds18b20(&oneWire);
HX711             hx711;
WiFiClient        wifiClient;
PubSubClient      mqttClient(wifiClient);

// Estado interno
unsigned long ultimaPublicacion = 0;
unsigned long ciclo             = 0;
int           nSensores         = 0;
DeviceAddress direccionesDS[6]; // almacena las direcciones ROM de los DS18B20

// =============================================================================
// SETUP
// =============================================================================
void setup() {
  Serial.begin(115200);
  delay(500);

  Serial.println("\n================================================");
  Serial.println("  GEMELO DIGITAL — Capa 1: Instrumentación");
  Serial.println("================================================");

  configurarPines();
  iniciarDS18B20();
  iniciarHX711();
  conectarWiFi();
  configurarMQTT();

  Serial.println("\n✅ Sistema listo. Iniciando publicación de datos...\n");
}

// =============================================================================
// LOOP PRINCIPAL
// =============================================================================
void loop() {
  // Mantener conexión MQTT activa
  if (!mqttClient.connected()) {
    reconectarMQTT();
  }
  mqttClient.loop();

  // Publicar datos según el intervalo configurado
  unsigned long ahora = millis();
  if (ahora - ultimaPublicacion >= INTERVALO_MS) {
    ultimaPublicacion = ahora;
    ciclo++;
    publicarDatos();
  }
}

// =============================================================================
// CONFIGURACIÓN DE PINES
// =============================================================================
void configurarPines() {
  pinMode(PIN_TRIG, OUTPUT);
  pinMode(PIN_ECHO, INPUT);
  digitalWrite(PIN_TRIG, LOW);
  Serial.println("✅ Pines configurados");
}

// =============================================================================
// DS18B20: INICIALIZACIÓN Y LECTURA
// =============================================================================
void iniciarDS18B20() {
  ds18b20.begin();
  nSensores = ds18b20.getDeviceCount();

  Serial.printf("✅ DS18B20: %d sensor(es) detectado(s) en bus 1-Wire\n", nSensores);

  if (nSensores == 0) {
    Serial.println("⚠️  Sin sensores DS18B20. Verificar:");
    Serial.println("   - Conexión DATA al GPIO 4");
    Serial.println("   - Resistencia pull-up 4.7kΩ entre DATA y 3.3V");
    Serial.println("   - Alimentación 3.3V y GND");
  }

  // Guardar dirección ROM de cada sensor y configurar resolución 12 bits
  for (int i = 0; i < nSensores && i < 6; i++) {
    ds18b20.getAddress(direccionesDS[i], i);
    ds18b20.setResolution(direccionesDS[i], 12);  // 0.0625°C, 750ms conversión

    Serial.printf("   Sensor %d — ROM: ", i);
    imprimirROM(direccionesDS[i]);
  }

  // Resolución global por si no se pudieron guardar direcciones
  ds18b20.setResolution(12);
}

void imprimirROM(DeviceAddress addr) {
  for (int i = 0; i < 8; i++) {
    if (addr[i] < 16) Serial.print("0");
    Serial.print(addr[i], HEX);
    if (i < 7) Serial.print(":");
  }
  Serial.println();
}

float leerTemperatura(int indice) {
  /*
   * Lee la temperatura del sensor en la posición 'indice' del bus.
   * Los sensores están ordenados por dirección ROM (orden físico en el bus).
   * IMPORTANTE: el orden del bus puede cambiar entre reinicios si los
   * sensores no tienen dirección asignada explícitamente. Para producción,
   * mapear cada dirección ROM a su posición física en el tanque.
   * Retorna -999.0 si hay error de lectura.
   */
  float temp = ds18b20.getTempCByIndex(indice);
  if (temp == DEVICE_DISCONNECTED_C) {
    Serial.printf("⚠️  Sensor %d desconectado\n", indice);
    return -999.0;
  }
  return temp;
}

// =============================================================================
// HC-SR04: NIVEL DE ACEITE
// =============================================================================
float leerNivelMetros() {
  /*
   * Mide la distancia desde la tapa hasta el pelo del aceite.
   * Nivel [m] = (ALTURA_TAPA_CM - distancia_cm) / 100
   *
   * Promedia 5 mediciones para reducir ruido ultrasónico.
   * Devuelve -1.0 si la lectura es inválida.
   *
   * Nota de hardware: el pin ECHO del HC-SR04 entrega 5V.
   * El ESP32 solo tolera 3.3V en sus GPIOs.
   * Es OBLIGATORIO usar el divisor resistivo:
   *   ECHO_HC → R1(1kΩ) → GPIO18
   *                     → R2(2kΩ) → GND
   */
  const int N_MUESTRAS   = 5;
  const long TIMEOUT_US  = 25000;  // 25ms → máx ~4.3m de distancia
  float suma = 0;
  int   validas = 0;

  for (int m = 0; m < N_MUESTRAS; m++) {
    // Pulso de disparo de 10µs
    digitalWrite(PIN_TRIG, LOW);
    delayMicroseconds(2);
    digitalWrite(PIN_TRIG, HIGH);
    delayMicroseconds(10);
    digitalWrite(PIN_TRIG, LOW);

    // Medir duración del eco
    long duracion = pulseIn(PIN_ECHO, HIGH, TIMEOUT_US);

    if (duracion > 0) {
      float distancia_cm = duracion * 0.0343 / 2.0;
      // Filtrar lecturas fuera del rango físico del tanque
      if (distancia_cm > 1.0 && distancia_cm < ALTURA_TAPA_CM) {
        suma += distancia_cm;
        validas++;
      }
    }
    delay(20);  // pausa entre mediciones para evitar rebotes
  }

  if (validas == 0) {
    Serial.println("⚠️  HC-SR04: sin lecturas válidas");
    return -1.0;
  }

  float distancia_media_cm = suma / validas;
  float nivel_cm = ALTURA_TAPA_CM - distancia_media_cm;
  return max(0.0f, nivel_cm / 100.0f);  // convertir a metros
}

// =============================================================================
// HX711 + CELDA DE CARGA: MASA TOTAL
// =============================================================================
void iniciarHX711() {
  hx711.begin(PIN_HX711_DT, PIN_HX711_SCK);
  hx711.set_scale(FACTOR_CALIBRACION);

  // Esperar a que el HX711 esté listo
  int intentos = 0;
  while (!hx711.is_ready() && intentos < 20) {
    delay(100);
    intentos++;
  }

  if (hx711.is_ready()) {
    hx711.tare(10);  // establecer tara promediando 10 lecturas
    Serial.println("✅ HX711: calibrado y tara establecida");
  } else {
    Serial.println("⚠️  HX711: no responde. Verificar conexiones DT/SCK");
  }
}

float leerMasaKg() {
  /*
   * Lee la masa bruta (aceite + tanque - tara) en kg.
   * Promedia 10 lecturas para estabilidad.
   * Retorna -1.0 si el HX711 no está disponible.
   */
  if (!hx711.is_ready()) return -1.0;

  float unidades = hx711.get_units(10);  // promedio de 10 lecturas
  float masa_kg  = unidades / 1000.0;    // el factor de calibración da gramos → convertir a kg

  // Filtro: rechazar lecturas claramente erróneas
  if (masa_kg < -0.5 || masa_kg > 60.0) {
    Serial.println("⚠️  HX711: lectura fuera de rango");
    return -1.0;
  }
  return masa_kg;
}

// =============================================================================
// PUBLICACIÓN MQTT
// =============================================================================
void publicarDatos() {
  /*
   * Secuencia de medición:
   *   1. Solicitar conversión a todos los DS18B20 (paralelo, 750ms)
   *   2. Mientras esperan, medir nivel y masa
   *   3. Leer temperaturas
   *   4. Empaquetar JSON y publicar
   */

  Serial.printf("\n── Ciclo %lu ──────────────────────────\n", ciclo);

  // ── Paso 1: iniciar conversión de temperatura (no bloqueante)
  ds18b20.setWaitForConversion(false);
  ds18b20.requestTemperatures();

  // ── Paso 2: medir nivel y masa mientras esperan los DS18B20
  float nivel_m  = leerNivelMetros();
  float masa_kg  = leerMasaKg();

  // ── Paso 3: esperar que termine la conversión DS18B20 (750ms a 12 bits)
  delay(800);

  // ── Paso 4: leer temperaturas
  float temps[6];
  for (int i = 0; i < nSensores && i < 6; i++) {
    temps[i] = leerTemperatura(i);
    Serial.printf("   T[%d] = %.4f°C\n", i, temps[i]);
  }
  Serial.printf("   Nivel = %.4f m\n", nivel_m);
  Serial.printf("   Masa  = %.4f kg\n", masa_kg);

  // ── Paso 5: construir JSON
  // Capacidad del documento: 6 temps + metadatos → 256 bytes es suficiente
  StaticJsonDocument<256> doc;

  doc["ts"]        = millis() / 1000;  // timestamp relativo en segundos
                                        // reemplazar por NTP si se necesita timestamp absoluto
  doc["ciclo"]     = ciclo;
  doc["nivel_m"]   = (nivel_m  >= 0) ? round(nivel_m  * 10000) / 10000.0 : -1;
  doc["masa_kg"]   = (masa_kg  >= 0) ? round(masa_kg  * 1000)  / 1000.0  : -1;
  doc["n_sensores"]= nSensores;

  JsonArray arrTemp = doc.createNestedArray("temp");
  for (int i = 0; i < nSensores && i < 6; i++) {
    if (temps[i] != -999.0)
      arrTemp.add(round(temps[i] * 1000) / 1000.0);  // redondear a 3 decimales
    else
      arrTemp.add(nullptr);  // null para sensor desconectado
  }

  // ── Paso 6: serializar y publicar
  char buffer[256];
  size_t n = serializeJson(doc, buffer);

  bool ok = mqttClient.publish(TOPIC_DATOS, buffer, n);
  Serial.printf("   MQTT publish → %s: %s (%d bytes)\n",
                TOPIC_DATOS, ok ? "✅ OK" : "❌ FALLO", n);

  // ── Paso 7: publicar heartbeat de estado
  StaticJsonDocument<128> estado;
  estado["ciclo"]     = ciclo;
  estado["heap_libre"]= ESP.getFreeHeap();
  estado["rssi_dbm"]  = WiFi.RSSI();
  estado["uptime_s"]  = millis() / 1000;

  char bufEstado[128];
  size_t nE = serializeJson(estado, bufEstado);
  mqttClient.publish(TOPIC_ESTADO, bufEstado, nE);
}

// =============================================================================
// WIFI
// =============================================================================
void conectarWiFi() {
  Serial.printf("\n📡 Conectando a Wi-Fi: %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int intentos = 0;
  while (WiFi.status() != WL_CONNECTED && intentos < 30) {
    delay(500);
    Serial.print(".");
    intentos++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n✅ Wi-Fi conectado — IP: %s | RSSI: %d dBm\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  } else {
    Serial.println("\n❌ No se pudo conectar al Wi-Fi. Reiniciando en 5s...");
    delay(5000);
    ESP.restart();
  }
}

// =============================================================================
// MQTT
// =============================================================================
void configurarMQTT() {
  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
  mqttClient.setCallback(callbackMQTT);
  mqttClient.setBufferSize(512);  // aumentar si el JSON crece
  reconectarMQTT();
}

void reconectarMQTT() {
  int intentos = 0;
  while (!mqttClient.connected() && intentos < 5) {
    Serial.printf("📡 Conectando a broker MQTT %s:%d...", MQTT_BROKER, MQTT_PORT);

    // Para broker sin autenticación:
    bool ok = mqttClient.connect(MQTT_CLIENT);
    // Para broker con usuario/contraseña, reemplazar por:
    // bool ok = mqttClient.connect(MQTT_CLIENT, MQTT_USER, MQTT_PASS);

    if (ok) {
      Serial.println(" ✅ Conectado");
      mqttClient.subscribe(TOPIC_CMD);  // suscribirse a comandos entrantes
    } else {
      Serial.printf(" ❌ Error %d — reintentando en 3s\n", mqttClient.state());
      delay(3000);
      intentos++;
    }
  }

  if (!mqttClient.connected()) {
    Serial.println("❌ No se pudo conectar al broker MQTT.");
    Serial.println("   Verificar: IP del broker, puerto 1883, red Wi-Fi.");
  }
}

void callbackMQTT(char* topic, byte* payload, unsigned int length) {
  /*
   * Manejo de comandos entrantes desde el servidor (Capa 2 → Capa 1).
   * Por ahora solo soporta el comando "tara" para recalibrar la celda de carga.
   * Se puede extender para cambiar el intervalo de muestreo, etc.
   */
  String mensaje = "";
  for (unsigned int i = 0; i < length; i++) {
    mensaje += (char)payload[i];
  }

  Serial.printf("📨 Comando recibido [%s]: %s\n", topic, mensaje.c_str());

  if (mensaje == "tara") {
    Serial.println("   → Estableciendo nueva tara...");
    hx711.tare(10);
    Serial.println("   → Tara actualizada ✅");
  }
  else if (mensaje.startsWith("intervalo:")) {
    // Formato: "intervalo:15000" para cambiar a 15 segundos
    // (no implementado aún — requiere variable global mutable)
    Serial.println("   → Cambio de intervalo (no implementado en v1.0)");
  }
}

// =============================================================================
// UTILIDAD: PROCEDIMIENTO DE CALIBRACIÓN DE CELDA DE CARGA
// =============================================================================
/*
 * Para calibrar la celda de carga correctamente:
 *
 * 1. Descomenta la llamada a calibrarCeldaCarga() en setup(), antes de iniciarHX711()
 * 2. Sube el firmware
 * 3. Abre el Monitor Serie a 115200 baudios
 * 4. Sigue las instrucciones en pantalla
 * 5. Anota el FACTOR_CALIBRACION que aparece
 * 6. Comenta nuevamente la llamada y actualiza la constante FACTOR_CALIBRACION arriba
 *
 * void calibrarCeldaCarga() {
 *   hx711.begin(PIN_HX711_DT, PIN_HX711_SCK);
 *   Serial.println("\n== CALIBRACIÓN CELDA DE CARGA ==");
 *   Serial.println("Retira todo peso del tanque y envía cualquier tecla...");
 *   while (!Serial.available()) delay(100);
 *   Serial.read();
 *   hx711.tare(20);
 *   Serial.println("Tara establecida.");
 *   Serial.println("Coloca una pesa de referencia conocida y envía su peso en gramos:");
 *   while (!Serial.available()) delay(100);
 *   float pesoRef = Serial.readStringUntil('\n').toFloat();
 *   float lectura = hx711.get_value(20);
 *   float factor  = lectura / pesoRef;
 *   Serial.printf("FACTOR_CALIBRACION = %.2f\n", factor);
 *   Serial.println("Copia este valor en la constante FACTOR_CALIBRACION del código.");
 * }
 */
