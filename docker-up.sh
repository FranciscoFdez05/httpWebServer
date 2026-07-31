#!/usr/bin/env sh
# Lanza el stack leyendo el puerto directamente de config.ini, para que
# el mapeo host:contenedor de docker-compose.yml (${PORT}) coincida
# siempre con el valor real que usa la app. Uso: ./docker-up.sh [args...]
set -e
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker no está instalado o no está en el PATH." >&2
    exit 1
fi

# Si no existe .env todavía (primer arranque en esta máquina), lo crea a
# partir de .env.example con una SECRET_KEY aleatoria ya generada, para
# no tener que hacerlo a mano.
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
    sed "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET_KEY|" .env.example > .env
    chmod 600 .env
    echo "Creado .env con una SECRET_KEY nueva generada automáticamente."
fi

# Puerto desde config.ini sin depender de python en el host (dentro del
# contenedor sí se usa configparser): solo se mira la clave "port" que
# esté bajo la sección [server].
PORT=$(awk -F= '
    /^[[:space:]]*\[/ { section=$0; gsub(/[][ \t\r]/, "", section); next }
    section == "server" {
        key=$1; gsub(/[ \t\r]/, "", key)
        if (key == "port") { val=$2; gsub(/[ \t\r]/, "", val); print val; exit }
    }
' config.ini)

case "$PORT" in
    ''|*[!0-9]*)
        echo "Aviso: config.ini no define un [server] port válido, se usa 8000." >&2
        PORT=8000
        ;;
esac
export PORT

docker compose up -d --build "$@"

# La app escucha en 0.0.0.0 dentro del contenedor y el puerto se publica
# en el host, así que cualquier equipo de la LAN entra por la IP de este
# servidor. Se muestran las IPs locales para no tener que buscarlas.
echo
echo "Servidor levantado en el puerto $PORT."
echo "Accesos desde la LAN:"
if command -v hostname >/dev/null 2>&1 && hostname -I >/dev/null 2>&1; then
    for ip in $(hostname -I); do echo "  http://$ip:$PORT"; done
elif command -v ip >/dev/null 2>&1; then
    ip -4 -o addr show scope global | awk -v p="$PORT" '{split($4,a,"/"); print "  http://" a[1] ":" p}'
else
    echo "  http://<IP-de-este-servidor>:$PORT"
fi
