FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# La base de datos y la configuración viven fuera del contenedor (volumen /datos)
ENV TRAZ_DATOS=/datos \
    TRAZ_PUERTO=8000 \
    PYTHONUNBUFFERED=1
VOLUME ["/datos"]
EXPOSE 8000

CMD ["python", "app.py"]
