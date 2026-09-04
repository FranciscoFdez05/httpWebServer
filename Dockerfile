FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ajustes.py tls.py healthcheck.py config.ini ./
COPY templates/ templates/
COPY static/ static/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# UID/GID fijo para el usuario no-root. Se crea /data (uploads + app.db +
# secret_key + tls) ya con este dueño: como ftp_data es un volumen con nombre,
# Docker copia el contenido/permisos de esta carpeta a la primera creación
# del volumen, así el proceso puede escribir en él sin correr como root.
#
# /data/tls con 700 porque ahí vive ca.key, la clave de la autoridad
# certificadora: quien la tenga puede suplantar a cualquier web ante los
# dispositivos que hayan instalado la CA.
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && groupadd -g 1000 appuser \
    && useradd -u 1000 -g appuser -M -s /usr/sbin/nologin appuser \
    && mkdir -p /data/uploads /data/tls \
    && chown -R appuser:appuser /data /app \
    && chmod 700 /data/tls

ENV DATA_DIR=/data

USER appuser

# Puerto informativo (documentación): el valor real siempre viene del entorno
# o de config.ini, tanto para el bind interno como para el mapeo de puertos en
# docker-compose.yml (ver ./docker-up.sh).
EXPOSE 8000

# El arranque —resolver el puerto y decidir si levantar con TLS— vive en
# docker-entrypoint.sh. Estaba en una línea CMD con un python -c dentro de
# comillas dentro de un sh -c, y al añadirle la decisión del TLS dejó de ser
# legible y de poder depurarse: un fallo ahí solo se ve como un contenedor
# reiniciando en bucle.
CMD ["docker-entrypoint.sh"]
