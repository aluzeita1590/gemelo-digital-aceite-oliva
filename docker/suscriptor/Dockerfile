FROM python:3.11-slim

WORKDIR /app

COPY docker/suscriptor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY capa2_adquisicion/suscriptor.py .
COPY config.py .

CMD ["python", "suscriptor.py"]
