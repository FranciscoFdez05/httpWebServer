#!/usr/bin/env sh
# Lanza el stack con el puerto y la versión que usa la propia app, para que
# el mapeo host:contenedor de docker-compose.yml (${PORT}) y la etiqueta de
# la imagen (${APP_VERSION}) no puedan desincronizarse del código.
# Para ACTUALIZAR una instalación ya en marcha usa ./docker-update.sh.
# Uso: ./docker-up.sh [args...]
set -e
cd "$(dirname "$0")"

SERVICIO="httpwebserver"

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker no está instalado o no está en el PATH." >&2
    exit 1
fi

# Puerto de fábrica, desde config.ini y sin depender de python en el host
# (dentro del contenedor sí se usa configparser): solo se mira la clave
# "port" que esté bajo la sección [server].
PORT_CONFIG_INI=$(awk -F= '
    /^[[:space:]]*\[/ { section=$0; gsub(/[][ \t\r]/, "", section); next }
    section == "server" {
        key=$1; gsub(/[ \t\r]/, "", key)
        if (key == "port") { val=$2; gsub(/[ \t\r]/, "", val); print val; exit }
    }
' config.ini)

# Si no existe .env todavía (primer arranque en esta máquina), lo crea a
# partir de .env.example con una SECRET_KEY aleatoria ya generada, para
# no tener que hacerlo a mano. El puerto se siembra desde config.ini, para
# que crear el .env no cambie por sorpresa el puerto de una instalación que
# ya tenía config.ini editado.
if [ ! -f .env ]; then
    if command -v openssl >/dev/null 2>&1; then
        SECRET_KEY=$(openssl rand -hex 32)
    else
        # Sin openssl: 64 hex desde el generador del kernel.
        SECRET_KEY=$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')
    fi
    if [ ${#SECRET_KEY} -lt 32 ]; then
        echo "Error: no se pudo generar una SECRET_KEY aleatoria." >&2
        exit 1
    fi
    case "$PORT_CONFIG_INI" in
        ''|*[!0-9]*) PUERTO_SEMILLA=8000 ;;
        *) PUERTO_SEMILLA="$PORT_CONFIG_INI" ;;
    esac
    sed -e "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET_KEY|" \
        -e "s|^PORT=.*|PORT=$PUERTO_SEMILLA|" .env.example > .env
    chmod 600 .env
    echo "Creado .env con una SECRET_KEY nueva generada automáticamente."
fi

# El puerto lo manda .env si lo define, y si no config.ini. Mismo criterio
# que app.py y que docker-update.sh: config.ini son los valores de fábrica
# (viaja con el código y se actualiza con él), y .env es la configuración de
# ESTA instalación, que no se versiona y sobrevive a los git pull.
PORT=$(sed -n 's/^[[:space:]]*PORT[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' .env 2>/dev/null | head -n 1)
[ -n "$PORT" ] || PORT="$PORT_CONFIG_INI"

case "$PORT" in
    ''|*[!0-9]*)
        echo "Aviso: no hay un puerto válido en .env ni en [server] de config.ini, se usa 8000." >&2
        PORT=8000
        ;;
esac
export PORT

# Etiqueta de la imagen. Sin esto cada build dejaría la imagen anterior sin
# nombre, y docker-update.sh no tendría ninguna versión a la que volver si la
# nueva no llega a arrancar.
APP_VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' app.py | head -n 1)
[ -n "$APP_VERSION" ] || APP_VERSION="dev"
export APP_VERSION

# Las IPs de este servidor en la LAN. Se pasan al contenedor porque desde
# dentro no se pueden averiguar —allí solo se ve la IP del puente de Docker— y
# son justo las que tiene que cubrir el certificado si se activa el HTTPS.
ips_de_la_lan() {
    if command -v hostname >/dev/null 2>&1 && hostname -I >/dev/null 2>&1; then
        hostname -I
    elif command -v ip >/dev/null 2>&1; then
        ip -4 -o addr show scope global | awk '{split($4,a,"/"); printf "%s ", a[1]}'
    fi
}
HOST_LAN_IPS=$(ips_de_la_lan)
export HOST_LAN_IPS

# El volumen es "external": lo crea el script, no Compose. Así "docker compose
# down -v" no puede borrar los archivos subidos ni la base de datos. Crearlo es
# idempotente: si ya existe, no hace nada.
docker volume create httpWebServer >/dev/null

docker compose up -d --build "$@"

# El esquema depende de si el HTTPS está activado, y ese estado vive en el
# volumen de datos (lo escribe la pantalla de Administración), no en ningún
# fichero del repositorio. Se le pregunta al contenedor, que es quien lo sabe.
if docker compose exec -T "$SERVICIO" test -f /data/tls/enabled 2>/dev/null; then
    ESQUEMA="https"
else
    ESQUEMA="http"
fi

# La app escucha en 0.0.0.0 dentro del contenedor y el puerto se publica
# en el host, así que cualquier equipo de la LAN entra por la IP de este
# servidor. Se muestran las IPs locales para no tener que buscarlas.
echo
echo "Servidor $APP_VERSION levantado en el puerto $PORT ($ESQUEMA)."
echo "Accesos desde la LAN:"
for ip in $(ips_de_la_lan); do
    echo "  $ESQUEMA://$ip:$PORT"
done
if [ -z "$(ips_de_la_lan)" ]; then
    echo "  $ESQUEMA://<IP-de-este-servidor>:$PORT"
fi

if [ "$ESQUEMA" = "http" ]; then
    echo
    echo "Va por HTTP sin cifrar: la contraseña y la sesión viajan en claro."
    echo "Para cifrarlo en la LAN, entra como administrador en /admin/https."
fi
