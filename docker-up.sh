#!/usr/bin/env sh
# Lanza el stack leyendo el puerto directamente de config.ini, para que
# el mapeo host:contenedor de docker-compose.yml (${PORT}) coincida
# siempre con el valor real que usa la app. Uso: ./docker-up.sh [args...]
set -e
cd "$(dirname "$0")"

# Si no existe .env todavía (primer arranque en esta máquina), lo crea a
# partir de .env.example con una SECRET_KEY aleatoria ya generada, para
# no tener que hacerlo a mano.
if [ ! -f .env ]; then
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    sed "s/^SECRET_KEY=.*/SECRET_KEY=$SECRET_KEY/" .env.example > .env
    echo "Creado .env con una SECRET_KEY nueva generada automáticamente."
fi

PORT=$(python3 -c "import configparser;c=configparser.ConfigParser();c.read('config.ini');print(c.getint('server','port',fallback=8000))")
export PORT

exec docker compose up -d --build "$@"
