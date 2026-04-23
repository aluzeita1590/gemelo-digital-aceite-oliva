# Imagen base Python slim para ARM64 (compatible Pi 5 y servidor x86)
FROM python:3.11-slim

WORKDIR /app

# Dependencias del sistema para matplotlib
RUN apt-get update && apt-get install -y \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Instalar dependencias Python
COPY docker/modelo/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el modelo y la configuración central
COPY capa3_modelo/modelo.py .
COPY config.py .

# Puerto Flask
EXPOSE 5000

CMD ["python", "modelo.py"]
