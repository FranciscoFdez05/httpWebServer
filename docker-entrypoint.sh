#!/usr/bin/env sh
# Arranque del contenedor.
#
# Antes esto era una línea CMD con un python -c dentro de comillas dentro de un
# sh -c. Al añadirle la decisión del TLS dejaba de ser legible y de poder
# depurarse: un fallo ahí solo se ve como un contenedor que reinicia en bucle.
set -e

# ── Puerto ────────────────────────────────────────────────────────────────────
# Mismo criterio que app.py: manda el entorno, y si no hay nada, config.ini.
if [ -z "$PORT" ]; then
    PORT=$(python -c "import configparser;c=configparser.ConfigParser();c.read('config.ini');print(c.getint('server','port',fallback=8000))")
fi

# ── TLS ───────────────────────────────────────────────────────────────────────
# Las tres condiciones son las mismas que comprueba tls.esta_activo(): el
# fichero-bandera que escribe la pantalla de Administración, y los dos ficheros
# del certificado. Si aquí y allí no coincidieran, la interfaz diría que el
# HTTPS está activo mientras el servidor sigue en HTTP.
TLS_DIR="${DATA_DIR:-/data}/tls"
SSL=""
ESQUEMA="http"

if [ "$HTTPS_ENABLED" = "false" ]; then
    # Salida de emergencia: si un certificado roto impide arrancar, esto
    # devuelve el servidor a HTTP sin tener que entrar a borrar ficheros.
    echo "HTTPS desactivado por HTTPS_ENABLED=false."
elif [ -f "$TLS_DIR/enabled" ] && [ -f "$TLS_DIR/server.crt" ] && [ -f "$TLS_DIR/server.key" ]; then
    SSL="--certfile=$TLS_DIR/server.crt --keyfile=$TLS_DIR/server.key"
    ESQUEMA="https"
    echo "HTTPS activado con el certificado de $TLS_DIR."
else
    echo "HTTPS no activado: se sirve por HTTP."
    echo "Para activarlo: entra como administrador en /admin/https."
fi

echo "Escuchando en ${ESQUEMA}://0.0.0.0:${PORT}"

# --timeout 0 desactiva el límite por petición: con subidas grandes en una red
# lenta, el timeout por defecto de gunicorn (30 s) corta la conexión a mitad
# de subida y se ve como un "WORKER TIMEOUT" sin más explicación.
exec gunicorn \
    --preload \
    --timeout 0 \
    --bind "0.0.0.0:${PORT}" \
    --workers 2 \
    $SSL \
    app:app
