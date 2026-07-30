FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY config.ini .
COPY templates/ templates/
COPY static/ static/

# UID/GID fijo para el usuario no-root. Se crea /data (uploads + app.db +
# secret_key) ya con este dueño: como ftp_data es un volumen con nombre,
# Docker copia el contenido/permisos de esta carpeta a la primera creación
# del volumen, así el proceso puede escribir en él sin correr como root.
RUN groupadd -g 1000 appuser \
    && useradd -u 1000 -g appuser -M -s /usr/sbin/nologin appuser \
    && mkdir -p /data/uploads \
    && chown -R appuser:appuser /data /app

ENV DATA_DIR=/data

USER appuser

# Puerto informativo (documentación): el valor real siempre viene de
# config.ini, tanto para este bind interno como para el mapeo de puertos
# en docker-compose.yml (ver ./docker-up.sh).
EXPOSE 8000

# Lee el puerto de config.ini en el propio arranque del contenedor, para
# no repetir el valor a mano en el Dockerfile.
# --timeout 0 desactiva el límite de tiempo por request: la app permite
# subidas sin límite de tamaño (ver MAX_CONTENT_LENGTH en app.py), y con
# el timeout por defecto de gunicorn (30s) un archivo grande en una red
# algo lenta corta la conexión a mitad de subida (WORKER TIMEOUT).
CMD ["sh", "-c", "PORT=$(python -c \"import configparser;c=configparser.ConfigParser();c.read('config.ini');print(c.getint('server','port',fallback=8000))\"); exec gunicorn --preload --timeout 0 --bind 0.0.0.0:$PORT --workers 2 app:app"]
